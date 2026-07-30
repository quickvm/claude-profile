"""claude-profile - Launch Claude Code with an isolated config directory."""

from __future__ import annotations

import base64
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib import resources
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
    podman_bin: str = Field(default="podman")
    sandbox_image: str = Field(default="claude-profile-sandbox:latest")
    sandbox_ram_mib: int = Field(default=4096)
    sandbox_cpus: int = Field(default=4)
    sandbox_skip_permissions: bool = Field(default=True)
    sandbox_ssh_agent: bool = Field(default=False)
    sandbox_gpg_agent: bool = Field(default=False)
    sandbox_clipboard: bool = Field(default=False)
    sandbox_chrome: bool = Field(default=False)
    sandbox_gh: bool = Field(default=False)
    # Allowlist of infisical logins to forward into the sandbox: comma-separated
    # emails or domain substrings (e.g. "corp.example,quickvm.com"). Empty = disabled.
    sandbox_infisical: str = Field(default="")
    sandbox_pulumi: bool = Field(default=False)
    # Comma-separated names of host env vars to copy into the sandbox (e.g. tokens that
    # host-oriented MCP servers pass through as `-e VAR`). Empty = none forwarded.
    sandbox_forward_env: str = Field(default="")
    # Per-launch override of the .sandbox marker (CLAUDE_PROFILE_SANDBOX). None = use marker.
    sandbox: Optional[bool] = Field(default=None)


settings = Settings()
console = Console()
err_console = Console(stderr=True)

LINKABLE_DIRS: tuple[str, ...] = ("commands", "skills")
SANDBOX_MARKER = ".sandbox"
SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
SANDBOX_CONFIG_DIR = "/home/appuser/.claude"
SANDBOX_GNUPGHOME = "/home/appuser/.gnupg"
# In-VM path where the host's Claude Code binary is mounted read-only. The entrypoint
# points the PATH entry at it, so the VM runs the host's version instead of the one
# baked into the image (see _sandbox_claude_binary).
SANDBOX_HOST_CLAUDE = "/opt/claude-host/claude"
# In-VM path where the shared, read-only MCP image store is mounted (additionalimagestore).
SANDBOX_IMAGE_STORE = "/var/lib/shared-mcp-store"
# Env var carrying the host clipboard-bridge TCP port to the in-VM wl-paste shim.
SANDBOX_CLIPBOARD_PORT_ENV = "CLAUDE_SANDBOX_CLIPBOARD_PORT"
# Env var carrying the host browser-bridge TCP port to the in-VM entrypoint.
SANDBOX_BROWSER_BRIDGE_PORT_ENV = "CLAUDE_SANDBOX_BROWSER_BRIDGE_PORT"
# Env var carrying the host browser-open TCP port to the in-VM google-chrome shim.
SANDBOX_BROWSER_OPEN_PORT_ENV = "CLAUDE_SANDBOX_BROWSER_OPEN_PORT"
# Chrome Web Store id of the Claude extension, and the env var naming the in-VM path to
# create so claude's extension detection (a readdir of
# <chrome-user-data>/<profile>/Extensions/<id>) succeeds inside the sandbox.
CHROME_EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
SANDBOX_CHROME_EXT_PATH_ENV = "CLAUDE_SANDBOX_CHROME_EXT_PATH"
# Chromium-family user-data dirs under ~/.config to look for the extension in.
CHROME_USER_DATA_DIRS: tuple[str, ...] = (
    "google-chrome",
    "chromium",
    "microsoft-edge",
    "BraveSoftware/Brave-Browser",
)
# OAuth scopes claude accepts for Claude in Chrome. It gates the integration on the token
# carrying one of these *before* every other enable condition, so a profile authenticated
# with a setup-token (which grants user:inference only) silently reports "Disabled".
CHROME_OAUTH_SCOPES: frozenset[str] = frozenset(
    {"user:profile", "user:office", "user:ccr_inference"}
)
# Deny-rule prefixes stripped from the in-VM settings overlay: host guardrails that
# are counterproductive inside the isolated microVM (deny beats
# --dangerously-skip-permissions, so they still apply there). The VM grants OS-level
# sudo scoped to dnf/podman, and only mounted paths exist inside it, so the host's
# blanket sudo deny and its ~/.ssh//~/.aws read guards just block the intended
# workflow (e.g. `sudo podman`, reading the forwarded ssh/known_hosts).
SANDBOX_STRIP_DENY_PREFIXES: tuple[str, ...] = (
    "Bash(sudo",
    "Read(~/.ssh",
    "Edit(~/.ssh",
    "Read(~/.aws",
)
# podman's default host.containers.internal address under pasta; mapping it to the
# host loopback lets the agent bridge bind to 127.0.0.1 instead of all interfaces.
SANDBOX_HOST_LOOPBACK = "169.254.1.2"
SANDBOX_BRIEFING = (
    "You are running inside the claude-profile microVM sandbox — an ephemeral "
    "podman/krun VM (confirm with /run/.containerenv). Only the mounted working "
    "directory and its git dir, plus this profile's Claude config, are visible; the "
    "rest of the host filesystem is not, which is why --dangerously-skip-permissions is "
    "safe here. Anything you install is discarded when the session ends, and you have "
    "passwordless sudo scoped to dnf and podman. To add a missing tool use `sudo dnf "
    "install <pkg>` or `uv tool install <tool>` (see the sandbox-tools skill). Nested "
    "containers run rootful automatically — just use `podman` (it is wrapped to sudo "
    "because rootless can't unpack layers in the VM's user namespace). If you need a tool made "
    "permanent, access outside the mounted paths, or anything the sandbox blocks, ask "
    "the user instead of working around it."
)
# Curated dev tools advertised by the sandbox-tools skill (command name -> description).
SANDBOX_SKILL_TOOLS: dict[str, str] = {
    "uv": "Python package/tool manager (uv tool install, uv run)",
    "ty": "ty (Python type checker)",
    "python3": "Python 3 (with pyyaml and jinja2)",
    "jq": "JSON processor",
    "yq": "YAML processor",
    "git": "Git",
    "gh": "GitHub CLI",
    "infisical": "Infisical CLI (secrets management)",
    "pulumi": "Pulumi (infrastructure as code)",
    "bk": "Buildkite CLI (auths via BUILDKITE_API_TOKEN)",
    "rg": "ripgrep (fast search)",
    "fd": "fd (fast file finder)",
    "make": "make",
    "shellcheck": "ShellCheck (shell linter)",
    "gcc": "C compiler",
    "openssl": "OpenSSL",
    "trash": "trash-cli (use instead of rm -rf)",
    "ssh": "OpenSSH client",
    "gpg": "GnuPG",
    "socat": "socat",
    "podman": "Podman — run nested containers (runs rootful automatically)",
    "node": "Node.js",
    "npm": "npm",
    "butane": "Butane (Ignition config compiler)",
}

app = typer.Typer(
    name="claude-profile",
    help="Launch Claude Code with isolated config directories per profile.",
    no_args_is_help=True,
)

# First-arg tokens that are subcommands, not profile names: main() treats any other
# non-flag first arg as a profile to launch, so every @app.command must be listed here
# (a test enforces this).
KNOWN_COMMANDS: frozenset[str] = frozenset(
    {
        "list",
        "add",
        "remove",
        "links",
        "env",
        "build",
        "sandbox",
        "sandbox-skill",
        "sandbox-cache",
    }
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
    table.add_column("Sandbox")

    for p in dirs:
        creds = p / ".credentials.json"
        if creds.exists():
            status = "[green]✓ authenticated[/green]"
        else:
            status = (
                f"[red]✗ not authenticated[/red] (run: claude-profile {p.name} /login)"
            )
        sandbox = "✓ microVM" if (p / SANDBOX_MARKER).exists() else "—"
        table.add_row(p.name, status, sandbox)

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
def add_profile(
    name: str = typer.Argument(..., help="Profile name to create"),
    sandbox: bool = typer.Option(
        False, "--sandbox", help="Run this profile in a microVM (podman + krun)."
    ),
) -> None:
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

    if sandbox:
        (d / SANDBOX_MARKER).touch()

    console.print(f"[green]Created profile '{name}'[/green] at {d}")
    if sandbox:
        console.print("[cyan]Sandbox mode: launches run in a microVM.[/cyan]")
        if not _sandbox_image_exists():
            console.print(
                f"Build the sandbox image first: claude-profile build "
                f"(image '{settings.sandbox_image}' not found)"
            )
    console.print(f"Authenticate with: claude-profile {name} /login")


def _sandbox_data_dir() -> Path:
    """Return the packaged sandbox build context (Containerfile + entrypoint.sh)."""
    return Path(str(resources.files("claude_profile") / "sandbox"))


def _sandbox_image_exists() -> bool:
    """Return True if the configured sandbox image is present locally."""
    try:
        result = subprocess.run(
            [settings.podman_bin, "image", "exists", settings.sandbox_image],
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _sandbox_image_user() -> str:
    """Return the sandbox image's configured USER (empty string means root)."""
    try:
        result = subprocess.run(
            [
                settings.podman_bin,
                "image",
                "inspect",
                settings.sandbox_image,
                "--format",
                "{{.Config.User}}",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@app.command("build")
def build_sandbox() -> None:
    """Build the microVM sandbox image (podman + krun)."""
    ctx = _sandbox_data_dir()
    cmd = [
        settings.podman_bin,
        "build",
        "-t",
        settings.sandbox_image,
        "-f",
        str(ctx / "Containerfile"),
        str(ctx),
    ]
    console.print(f"Building [cyan]{settings.sandbox_image}[/cyan] ...")
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        err_console.print(
            f"[red]'{settings.podman_bin}' not found.[/red] "
            f"Install podman and crun-krun (dnf install crun-krun)."
        )
        raise typer.Exit(code=1) from None
    except subprocess.CalledProcessError as exc:
        err_console.print(f"[red]Build failed (exit {exc.returncode}).[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Built {settings.sandbox_image}.[/green]")


def _sandbox_installed_tools() -> list[str]:
    """Return which curated dev tools are present in the sandbox image."""
    names = " ".join(SANDBOX_SKILL_TOOLS)
    script = f'for t in {names}; do command -v "$t" >/dev/null 2>&1 && echo "$t"; done'
    result = subprocess.run(
        [
            settings.podman_bin,
            "run",
            "--rm",
            settings.sandbox_image,
            "sh",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
    )
    present = set(result.stdout.split())
    return [tool for tool in SANDBOX_SKILL_TOOLS if tool in present]


def _render_sandbox_skill(tools: list[str]) -> str:
    """Render the sandbox-tools SKILL.md with the given installed-tools list."""
    template = (
        resources.files("claude_profile") / "sandbox_skill_template.md"
    ).read_text()
    listing = "\n".join(f"- `{tool}` — {SANDBOX_SKILL_TOOLS[tool]}" for tool in tools)
    return template.replace("{{INSTALLED_TOOLS}}", listing)


@app.command("sandbox-skill")
def sandbox_skill(
    check: bool = typer.Option(
        False, "--check", help="Verify the skill matches the image; exit 1 if stale."
    ),
    path: Optional[Path] = typer.Option(
        None,
        "--path",
        help="SKILL.md path (default ~/.claude/skills/sandbox-tools/SKILL.md).",
    ),
) -> None:
    """Write (or --check) the sandbox-tools skill from the image's installed tools."""
    dest = path or (Path.home() / ".claude" / "skills" / "sandbox-tools" / "SKILL.md")
    _ensure_sandbox_image()
    content = _render_sandbox_skill(_sandbox_installed_tools())
    if check:
        current = dest.read_text() if dest.exists() else ""
        if current != content:
            err_console.print(
                f"[red]{dest} is out of date.[/red] Run: claude-profile sandbox-skill"
            )
            raise typer.Exit(code=1)
        console.print(f"[green]{dest} is up to date.[/green]")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    console.print(f"[green]Wrote {dest}[/green]")


def _data_dir() -> Path:
    """Host directory holding claude-profile's own cached data (XDG data)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "claude-profile"


def _image_cache_dir() -> Path:
    """Host directory holding the shared, read-only MCP image store."""
    return _data_dir() / "image-store"


def _image_ref_from_args(args: list) -> Optional[str]:
    """Pick the container image ref out of a podman/docker ``run`` arg list.

    The image is the first arg that is not a flag or path and whose first path segment
    looks like a registry host (has a ``.`` or ``:``), which distinguishes a
    fully-qualified ref like ``ghcr.io/o/i:tag`` from ``-v``/``-e`` flag values.
    """
    for arg in args:
        if not isinstance(arg, str) or arg.startswith(("-", "/")) or "/" not in arg:
            continue
        host = arg.split("/", 1)[0]
        if "." in host or ":" in host:
            return arg
    return None


def _mcp_container_images(profile_dir: Path) -> list[str]:
    """Return the image refs used by the profile's podman/docker MCP servers.

    Reads the profile's ``.claude.json`` (user-scope and per-project ``mcpServers``)
    and extracts the image from each stdio server run via podman/docker. De-duplicated.
    """
    config = profile_dir / ".claude.json"
    try:
        data = json.loads(config.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    blocks = [data.get("mcpServers")]
    projects = data.get("projects")
    if isinstance(projects, dict):
        blocks += [
            p.get("mcpServers") for p in projects.values() if isinstance(p, dict)
        ]
    images: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for server in block.values():
            if not isinstance(server, dict) or server.get("command") not in (
                "podman",
                "docker",
            ):
                continue
            image = _image_ref_from_args(server.get("args") or [])
            if image and image not in images:
                images.append(image)
    return images


def _image_store_populated(store: Path) -> bool:
    """True if the host image store has been populated with images."""
    return (store / "overlay-images").is_dir()


def _run_checked(cmd: list[str]) -> None:
    """Run a command, exiting with a clear message on failure."""
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        err_console.print(f"[red]'{cmd[0]}' not found.[/red]")
        raise typer.Exit(code=1) from None
    except subprocess.CalledProcessError as exc:
        err_console.print(f"[red]{cmd[0]} failed (exit {exc.returncode}).[/red]")
        raise typer.Exit(code=1) from None


def _cache_driver_args(fuse: str) -> list[str]:
    return [
        "--storage-driver",
        "overlay",
        "--storage-opt",
        f"overlay.mount_program={fuse}",
    ]


def _cache_pull(store: Path, fuse: str, images: list[str]) -> None:
    """Pull images into the store and make it readable by the VM's mapped root."""
    store.mkdir(parents=True, exist_ok=True)
    console.print(f"Caching {len(images)} image(s) into [cyan]{store}[/cyan] ...")
    for image in images:
        console.print(f"  pulling {image}")
        _run_checked(
            [
                settings.podman_bin,
                "--root",
                str(store),
                *_cache_driver_args(fuse),
                "pull",
                image,
            ]
        )
    # The VM's rootful podman runs as a mapped uid, so the store must be world-readable.
    _run_checked([settings.podman_bin, "unshare", "chmod", "-R", "a+rX", str(store)])
    console.print(
        f"[green]Cached {len(images)} image(s); the sandbox mounts them read-only.[/green]"
    )


@app.command("sandbox-cache")
def sandbox_cache(
    name: str = typer.Argument(..., help="Profile whose MCP images to cache"),
    clear: bool = typer.Option(False, "--clear", help="Empty the image cache instead."),
) -> None:
    """Pre-pull a profile's podman-run MCP images into a shared store the sandbox mounts
    read-only, so they are not re-pulled on every microVM launch."""
    store = _image_cache_dir()
    fuse = shutil.which("fuse-overlayfs")
    if fuse is None:
        err_console.print(
            "[red]'fuse-overlayfs' not found on host.[/red] Install it "
            "(dnf install fuse-overlayfs)."
        )
        raise typer.Exit(code=1)
    if clear:
        if store.exists():
            _run_checked(
                [
                    settings.podman_bin,
                    "--root",
                    str(store),
                    *_cache_driver_args(fuse),
                    "system",
                    "reset",
                    "--force",
                ]
            )
        console.print(f"[green]Image cache cleared ({store}).[/green]")
        return
    profile_dir = settings.profiles_base / name
    if not profile_dir.exists():
        err_console.print(f"[red]Profile '{name}' does not exist.[/red]")
        raise typer.Exit(code=1)
    images = _mcp_container_images(profile_dir)
    if not images:
        console.print(f"No podman/docker MCP images found in profile '{name}'.")
        return
    _cache_pull(store, fuse, images)


@app.command("sandbox")
def manage_sandbox(
    name: str = typer.Argument(..., help="Profile name"),
    on: bool = typer.Option(False, "--on", help="Enable sandbox (microVM) mode."),
    off: bool = typer.Option(
        False, "--off", help="Disable sandbox mode (run on host)."
    ),
) -> None:
    """Enable, disable, or show microVM sandbox mode for a profile."""
    if on and off:
        err_console.print("[red]Error: --on and --off are mutually exclusive.[/red]")
        raise typer.Exit(code=1)

    d = settings.profiles_base / name
    if not d.exists():
        err_console.print(f"[red]Profile '{name}' does not exist.[/red]")
        raise typer.Exit(code=1)

    marker = d / SANDBOX_MARKER
    if not on and not off:
        state = "microVM" if marker.exists() else "host"
        console.print(f"Profile '{name}' launches on: {state}")
        return

    if on:
        marker.touch()
        console.print(f"[green]Sandbox enabled for '{name}'.[/green]")
        if not _sandbox_image_exists():
            console.print("Build the image first: claude-profile build")
    else:
        marker.unlink(missing_ok=True)
        console.print(f"[green]Sandbox disabled for '{name}' (runs on host).[/green]")


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


@app.command("env")
def manage_env(
    name: str = typer.Argument(..., help="Profile name"),
    set_var: Optional[list[str]] = typer.Option(
        None, "--set", help="Set a variable: KEY=VALUE"
    ),
    unset_var: Optional[list[str]] = typer.Option(
        None, "--unset", help="Unset a variable by name"
    ),
) -> None:
    """Manage per-profile environment variables stored in .env."""
    d = settings.profiles_base / name
    if not d.exists():
        err_console.print(f"[red]Profile '{name}' does not exist.[/red]")
        raise typer.Exit(code=1)

    env_file = d / ".env"
    existing = _parse_env_file(env_file) if env_file.exists() else {}

    if not set_var and not unset_var:
        _show_env_table(name, existing)
        return

    if set_var:
        for entry in set_var:
            if "=" not in entry:
                err_console.print(
                    f"[red]Error: '{entry}' is not valid. Use KEY=VALUE.[/red]"
                )
                raise typer.Exit(code=1)
            key, _, value = entry.partition("=")
            existing[key.strip()] = value.strip()

    if unset_var:
        for key in unset_var:
            if key not in existing:
                err_console.print(f"[yellow]Warning: '{key}' is not set.[/yellow]")
                continue
            del existing[key]

    _write_env_file(env_file, existing)
    console.print(f"[green]Updated .env for profile '{name}'.[/green]")


def _show_env_table(name: str, env_vars: dict[str, str]) -> None:
    if not env_vars:
        console.print(f"No environment variables set for profile '{name}'.")
        return
    table = Table(title=f"Environment for profile '{name}'")
    table.add_column("Variable", style="cyan")
    table.add_column("Value")
    for key, value in sorted(env_vars.items()):
        table.add_row(key, value)
    console.print(table)


def _write_env_file(env_file: Path, env_vars: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in sorted(env_vars.items())]
    env_file.write_text("\n".join(lines) + "\n" if lines else "")


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


def _parse_env_file(env_path: Path) -> dict[str, str]:
    """Parse a .env file into a dict of KEY=VALUE pairs.

    Skips blank lines and comments. Strips optional quoting from values.
    """
    result: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def _ensure_sandbox_image() -> None:
    """Exit with a clear message if the sandbox image has not been built."""
    if not _sandbox_image_exists():
        err_console.print(
            f"[red]Sandbox image '{settings.sandbox_image}' not found.[/red] "
            f"Build it with: claude-profile build"
        )
        sys.exit(1)


def _git_common_dir(cwd: Path) -> Optional[Path]:
    """Return the absolute git common dir for cwd, or None if not in a repo.

    For a worktree this is the main repo's .git dir, which lives outside the
    worktree and must be mounted so git works inside the microVM.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        return None
    common = Path(out)
    return common.resolve() if common.is_absolute() else (cwd / common).resolve()


def _sandbox_settings_overlay(profile_dir: Path) -> Optional[Path]:
    """Write a sandbox-tuned settings.json (host sudo deny stripped) to mount in the VM.

    The profile's settings.json is copied from the host and carries host-oriented deny
    rules (e.g. ``Bash(sudo *)``) that still apply inside the VM — deny wins even under
    --dangerously-skip-permissions. The microVM is the isolation boundary and grants
    scoped sudo, so we strip those denies into an overlay mounted only in the VM; the
    profile's real settings.json (used by host launches) is untouched. Returns the
    overlay path, or None when there is no settings.json or nothing to strip.
    """
    src = profile_dir / "settings.json"
    if not src.exists():
        return None
    try:
        data = json.loads(src.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    perms = data.get("permissions")
    if not isinstance(perms, dict) or not isinstance(perms.get("deny"), list):
        return None
    deny = perms["deny"]
    kept = [
        rule
        for rule in deny
        if not (isinstance(rule, str) and rule.startswith(SANDBOX_STRIP_DENY_PREFIXES))
    ]
    if len(kept) == len(deny):
        return None
    perms["deny"] = kept
    overlay = profile_dir / "settings.sandbox.json"
    try:
        overlay.write_text(json.dumps(data, indent=2))
    except OSError:
        return None
    return overlay


def _sandbox_chrome_overlay(profile_dir: Path) -> Path:
    """Throwaway dir mounted over the profile's ``chrome/`` inside the VM.

    The profile is mounted as the in-VM config dir, so claude's "Install Chrome extension"
    run *inside* the sandbox rewrites ``chrome/chrome-native-host`` to an in-VM path
    (``/home/appuser/...``). Chrome's native-messaging manifest on the host points at that
    same wrapper, so the in-VM install silently breaks the **host's** Chrome integration —
    Chrome can no longer spawn the native host. Masking the dir keeps in-VM installs inside
    the VM while leaving the host's wrapper intact.
    """
    overlay = profile_dir / "chrome.sandbox"
    overlay.mkdir(exist_ok=True)
    return overlay


def _storage_cache_conf(profile_dir: Path) -> Path:
    """Write the storage.conf overlay adding the mounted store as a read-only
    additionalimagestore (regenerated each launch), and return its path."""
    conf = profile_dir / "storage.sandbox.conf"
    conf.write_text(
        "[storage]\n"
        'driver = "overlay"\n'
        'graphroot = "/var/lib/containers/storage"\n'
        'runroot = "/run/containers/storage"\n'
        "[storage.options]\n"
        f'additionalimagestores = ["{SANDBOX_IMAGE_STORE}"]\n'
        "[storage.options.overlay]\n"
        'mount_program = "/usr/bin/fuse-overlayfs"\n'
    )
    return conf


def _image_cache_mounts(profile_dir: Path) -> list[str]:
    """Mounts exposing the shared MCP image store to the VM, when it is populated.

    Bind-mounts the host store read-only at the additionalimagestore path and a
    storage.conf overlay pointing podman at it, so podman/MCP servers find images
    locally instead of pulling. Returns [] when the store is empty/absent, leaving
    the image's default storage config (pull-on-demand) in place.
    """
    store = _image_cache_dir()
    if not _image_store_populated(store):
        return []
    conf = _storage_cache_conf(profile_dir)
    return [
        "-v",
        f"{store}:{SANDBOX_IMAGE_STORE}:ro,z",
        "-v",
        f"{conf}:/etc/containers/storage.conf:ro,z",
    ]


def _sandbox_known_hosts(profile_dir: Path) -> Path:
    """Per-profile user known_hosts the sandbox records accepted host keys into.

    Created empty if absent and mounted read-write, so host keys ssh accepts inside the
    VM persist across launches. The host's own known_hosts is mounted read-only as the
    global known_hosts (see _sandbox_mounts), so already-trusted hosts still verify and
    the host's real file is never written by the sandbox.
    """
    dest = profile_dir / "known_hosts"
    if not dest.exists():
        dest.touch()
    return dest


def _host_claude_binary() -> Optional[Path]:
    """The host's Claude Code binary, when it came from the native installer.

    ``claude.ai/install.sh`` drops a self-contained executable at
    ``<data dir>/claude/versions/<version>`` and points ``claude`` on PATH at it, so that
    one file runs anywhere with a glibc — including inside the sandbox. Returns None for
    every other install method (npm, a distro package), whose entry point is a launcher
    that needs the rest of its tree, leaving the image's own claude to run.
    """
    found = shutil.which(settings.claude_bin)
    if found is None:
        return None
    binary = Path(found).resolve()
    if binary.parent.name != "versions" or binary.parent.parent.name != "claude":
        return None
    return binary if os.access(binary, os.X_OK) else None


def _sandbox_claude_binary() -> Optional[Path]:
    """Cache the host's Claude Code binary for the VM and return the cached copy.

    Without this the sandbox runs whatever version was baked into the image, which ages
    with every release until someone rebuilds; mounting the host's binary makes each VM
    track the host's own auto-updated install. The mount needs an SELinux relabel (``:z``)
    to be readable in the VM, and relabelling the user's real install is not ours to do,
    so the binary is copied into our data dir and that copy is relabelled instead. Version
    directories are immutable, so the copy happens only when the host updates; the
    previous version is pruned. Returns None when the host has no native install.
    """
    source = _host_claude_binary()
    if source is None:
        return None
    cache = _data_dir() / "claude"
    dest = cache / source.name
    # Named per process: sandboxes launch in parallel (one per worktree), and two of them
    # sharing a temp file would interleave writes into one torn binary.
    partial = cache / f".{source.name}.{os.getpid()}.partial"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            # Copy to a temp name and rename, so an interrupted launch can't leave a
            # truncated binary that later launches would mistake for a complete one.
            shutil.copy(source, partial)
            os.replace(partial, dest)
        for stale in cache.iterdir():
            if stale not in (dest, partial):
                stale.unlink()
    except OSError as exc:
        err_console.print(
            f"[yellow]Warning: could not cache {source} for the sandbox ({exc}); "
            f"the VM will run the image's own claude.[/yellow]"
        )
        return None
    return dest


def _sandbox_mounts(profile_dir: Path, cwd: Path) -> list[str]:
    """Build podman -v args: profile config, cwd, and the git common dir.

    Mounts use ``:z`` (SELinux relabel) only. ``:U`` is deliberately omitted:
    ``--userns=keep-id`` already maps the host UID into the VM, while ``:U`` would
    recursively chown the mounted tree to the container's run-user (root, which maps
    to a subuid), wrecking ownership of the user's project on the host.
    """
    mounts = [
        "-v",
        f"{profile_dir}:{SANDBOX_CONFIG_DIR}:z",
        "-v",
        f"{cwd}:{cwd}:z",
    ]
    # Mask the profile's chrome/ dir: an in-VM native-host install must not rewrite the
    # host's wrapper, which Chrome's manifest points at (see _sandbox_chrome_overlay).
    mounts += [
        "-v",
        f"{_sandbox_chrome_overlay(profile_dir)}:{SANDBOX_CONFIG_DIR}/chrome:z",
    ]
    overlay = _sandbox_settings_overlay(profile_dir)
    if overlay is not None:
        # Override just settings.json inside the VM; writes land in the throwaway
        # overlay (regenerated each launch), not the profile's real settings.json.
        mounts += ["-v", f"{overlay}:{SANDBOX_CONFIG_DIR}/settings.json:z"]
    git_dir = _git_common_dir(cwd)
    if git_dir is not None and git_dir != cwd and cwd not in git_dir.parents:
        mounts += ["-v", f"{git_dir}:{git_dir}:z"]
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        mounts += ["-v", f"{gitconfig}:/home/appuser/.gitconfig:ro,z"]
    host_known_hosts = Path.home() / ".ssh" / "known_hosts"
    if host_known_hosts.exists():
        # Read-only *global* known_hosts: ssh verifies already-trusted hosts against it
        # but never writes it, so the sandbox can't modify the host's real file.
        mounts += ["-v", f"{host_known_hosts}:/etc/ssh/ssh_known_hosts:ro,z"]
    # Writable per-profile *user* known_hosts: ssh records newly accepted host keys here,
    # so they persist across launches instead of vanishing with the VM.
    mounts += [
        "-v",
        f"{_sandbox_known_hosts(profile_dir)}:/home/appuser/.ssh/known_hosts:z",
    ]
    host_claude = _sandbox_claude_binary()
    if host_claude is not None:
        # Run the host's current claude instead of the image's baked one, so the sandbox
        # follows the host's auto-updates (see _sandbox_claude_binary).
        mounts += ["-v", f"{host_claude}:{SANDBOX_HOST_CLAUDE}:ro,z"]
    mounts += _linked_dir_mounts(profile_dir)
    mounts += _image_cache_mounts(profile_dir)
    return mounts


def _linked_dir_mounts(profile_dir: Path) -> list[str]:
    """Read-only mounts for any LINKABLE_DIRS the profile symlinks to global dirs.

    The profile dir is mounted as the in-VM config dir, but a symlinked
    commands/skills points at an absolute host path (e.g. ~/.claude/skills) that is
    not otherwise mounted, so the link dangles inside the VM. Mount the real target
    at the link's path, read-only so a sandboxed agent cannot modify dirs shared
    across every profile.
    """
    mounts: list[str] = []
    for dir_name in LINKABLE_DIRS:
        link = profile_dir / dir_name
        if not link.is_symlink():
            continue
        target = Path(os.readlink(link))
        if not target.is_absolute():
            continue
        real = link.resolve()
        if real.exists():
            mounts += ["-v", f"{real}:{target}:ro,z"]
    return mounts


@dataclass
class _Forwarding:
    """Plan for bridging host agents into the VM via socat over pasta."""

    forwards: list[tuple[Path, Path, int]]  # (host_socket, guest_path, tcp_port)
    ssh_auth_sock: Optional[Path] = None
    gpg_pubkeys_b64: Optional[str] = None
    clipboard_port: Optional[int] = None  # host TCP port serving the clipboard bridge
    browser_port: Optional[int] = (
        None  # host TCP port serving the Claude in Chrome socket bridge
    )
    browser_open_port: Optional[int] = (
        None  # host TCP port serving the browser-open bridge
    )

    def active(self) -> bool:
        """True when any host-side bridge (agent socket, clipboard, browser) is needed."""
        return (
            bool(self.forwards)
            or self.clipboard_port is not None
            or self.browser_port is not None
            or self.browser_open_port is not None
        )


def _build_sandbox_argv(
    profile_dir: Path,
    cwd: Path,
    claude_args: list[str],
    extra_env: dict[str, str],
    forwarding: Optional[_Forwarding] = None,
) -> list[str]:
    """Assemble the `podman run` argv that boots claude in a krun microVM."""
    argv = [
        settings.podman_bin,
        "run",
        "--rm",
        "-it",
        "--annotation",
        "run.oci.handler=krun",
        "--annotation",
        f"krun.ram_mib={settings.sandbox_ram_mib}",
        "--annotation",
        f"krun.cpus={settings.sandbox_cpus}",
        # passt networking (virtio-net + a real guest kernel netstack) instead of
        # libkrun's default TSI socket impersonation. TSI stubs setsockopt — SO_REUSEADDR
        # reads back 0, which aborts gRPC (and any set-then-verify sockopt) — and
        # intercepts the guest's AF_INET sockets, which breaks nested-container DNS.
        # passt fixes both. Needs `passt` on the host and a crun/libkrun with passt
        # support (crun >= 1.21-ish, libkrun >= 1.9); older runtimes ignore it (→ TSI).
        "--annotation",
        "krun.use_passt=1",
        "--userns=keep-id",
        "--device",
        "/dev/kvm",
    ]
    if forwarding and forwarding.active():
        # pasta gives the VM a route to the host (TSI cannot); --map-host-loopback
        # makes host.containers.internal reach the host's loopback, so the agent
        # bridge can bind to 127.0.0.1 rather than every host interface.
        argv.append(f"--network=pasta:--map-host-loopback,{SANDBOX_HOST_LOOPBACK}")
    argv += [
        "-e",
        f"HOST_UID={os.getuid()}",
        "-e",
        f"HOST_GID={os.getgid()}",
        "-e",
        f"CLAUDE_CONFIG_DIR={SANDBOX_CONFIG_DIR}",
        "-e",
        "TERM",
        "-e",
        "COLORTERM",
    ]
    if _host_claude_binary() is not None:
        # The VM runs the host's binary from a read-only mount and is thrown away at
        # exit, so an in-VM self-update would download a release only to discard it —
        # and would move the session off the host's version mid-run.
        argv += ["-e", "DISABLE_AUTOUPDATER=1"]
    argv += _forwarding_env(forwarding)
    for key, value in extra_env.items():
        argv += ["-e", f"{key}={value}"]
    argv += _sandbox_mounts(profile_dir, cwd)
    argv += ["-w", str(cwd), settings.sandbox_image, "claude"]
    args = list(claude_args)
    if settings.sandbox_skip_permissions and SKIP_PERMISSIONS_FLAG not in args:
        args.append(SKIP_PERMISSIONS_FLAG)
    # claude force-disables Chrome in a non-interactive session (dn()=!isInteractive),
    # which the sandbox launch trips, so claudeInChromeDefaultEnabled never applies. The
    # explicit --chrome flag is checked first, so append it to actually enable the
    # integration when the user opted into sandbox_chrome.
    if settings.sandbox_chrome and "--chrome" not in args and "--no-chrome" not in args:
        args.append("--chrome")
    if "--append-system-prompt" not in args:
        args += [
            "--append-system-prompt",
            SANDBOX_BRIEFING + _infisical_briefing(extra_env),
        ]
    return argv + args


def _forwarding_env(forwarding: Optional[_Forwarding]) -> list[str]:
    """Env args telling the entrypoint which sockets to bridge and how."""
    if forwarding is None or not forwarding.active():
        return []
    env: list[str] = []
    if forwarding.forwards:
        spec = ",".join(f"{guest}={port}" for _host, guest, port in forwarding.forwards)
        env += ["-e", f"CLAUDE_SANDBOX_FORWARDS={spec}"]
    if forwarding.ssh_auth_sock is not None:
        env += ["-e", f"SSH_AUTH_SOCK={forwarding.ssh_auth_sock}"]
    if forwarding.gpg_pubkeys_b64 is not None:
        env += [
            "-e",
            f"GNUPGHOME={SANDBOX_GNUPGHOME}",
            "-e",
            f"CLAUDE_SANDBOX_GPG_PUBKEYS={forwarding.gpg_pubkeys_b64}",
        ]
    if forwarding.clipboard_port is not None:
        env += ["-e", f"{SANDBOX_CLIPBOARD_PORT_ENV}={forwarding.clipboard_port}"]
    if forwarding.browser_port is not None:
        env += ["-e", f"{SANDBOX_BROWSER_BRIDGE_PORT_ENV}={forwarding.browser_port}"]
    if forwarding.browser_open_port is not None:
        env += ["-e", f"{SANDBOX_BROWSER_OPEN_PORT_ENV}={forwarding.browser_open_port}"]
    return env


def _free_tcp_port() -> int:
    """Return an unused localhost TCP port for an agent bridge."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ssh_agent_status(sock: Path) -> int:
    """Probe an ssh-agent socket: 2 = live with keys, 1 = live but empty, 0 = dead.

    ``ssh-add -l`` exits 0 when it lists keys, 1 when the agent is live but has none, and
    2 when it cannot connect — a stale/dead socket (e.g. a gnome-keyring stub whose agent
    isn't running, common when the real keys live in 1Password). Forwarding a dead socket
    puts a broken agent behind the VM's SSH_AUTH_SOCK ("communication with agent failed"),
    and forwarding a live-but-empty one first would shadow the agent that actually holds
    the keys — so callers skip dead sockets and prefer keyed ones.
    """
    try:
        result = subprocess.run(
            ["ssh-add", "-l"],
            env={**os.environ, "SSH_AUTH_SOCK": str(sock)},
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    return {0: 2, 1: 1}.get(result.returncode, 0)


def _ssh_agent_sockets() -> list[Path]:
    """Live host SSH agent sockets to bridge, agents holding keys first.

    Candidates are the active agent (SSH_AUTH_SOCK) and the 1Password agent. A socket
    file can exist while its agent is dead (a stale gnome-keyring stub), so each is
    probed: dead ones are dropped and the rest are ordered keyed-agents-first, so the
    VM's SSH_AUTH_SOCK lands on an agent that actually has keys.
    """
    candidates: list[Path] = []
    auth = os.environ.get("SSH_AUTH_SOCK")
    if auth:
        candidates.append(Path(auth))
    onepassword = Path.home() / ".1password" / "agent.sock"
    if onepassword not in candidates:
        candidates.append(onepassword)
    live = [
        (sock, status)
        for sock in candidates
        if sock.exists() and (status := _ssh_agent_status(sock)) > 0
    ]
    live.sort(
        key=lambda pair: -pair[1]
    )  # stable: keyed agents first, else insertion order
    return [sock for sock, _ in live]


def _gpg_extra_socket() -> Optional[Path]:
    """Path to the host gpg-agent restricted (signing-only) socket, if present."""
    try:
        result = subprocess.run(
            ["gpgconf", "--list-dirs", "agent-extra-socket"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    path = result.stdout.strip()
    if result.returncode != 0 or not path:
        return None
    sock = Path(path)
    return sock if sock.exists() else None


def _export_gpg_pubkeys() -> Optional[str]:
    """Base64-encoded export of the host public keyring (no secret material)."""
    try:
        result = subprocess.run(["gpg", "--export"], capture_output=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return base64.b64encode(result.stdout).decode("ascii")


def _browser_bridge_dir() -> Path:
    """Host dir where Claude in Chrome native hosts bind their sockets.

    Chrome spawns ``claude --chrome-native-host`` (stdio to the extension), which
    binds ``/tmp/claude-mcp-browser-bridge-<username>/<pid>.sock``; claude sessions
    discover the bridge by scanning that dir at startup. The username comes from the
    uid, matching claude's own ``os.userInfo().username``.
    """
    return Path(f"/tmp/claude-mcp-browser-bridge-{pwd.getpwuid(os.getuid()).pw_name}")


def _browser_bridge_live() -> bool:
    """True when some Claude in Chrome native-host socket accepts connections.

    A socket file can outlive its native host (nothing unlinks it after a crash or
    browser exit), so each candidate is probed with a real connect — a directory
    holding only stale sockets means no bridge.
    """
    for sock_path in sorted(_browser_bridge_dir().glob("*.sock")):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(str(sock_path))
        except OSError:
            continue
        finally:
            probe.close()
        return True
    return False


def _build_forwarding() -> _Forwarding:
    """Collect the agent forwards requested via settings."""
    forwards: list[tuple[Path, Path, int]] = []
    ssh_auth: Optional[Path] = None
    if settings.sandbox_ssh_agent:
        ssh = [(sock, sock, _free_tcp_port()) for sock in _ssh_agent_sockets()]
        forwards += ssh
        if ssh:
            ssh_auth = ssh[0][1]
    pubkeys: Optional[str] = None
    if settings.sandbox_gpg_agent:
        extra = _gpg_extra_socket()
        if extra is not None:
            guest = Path(SANDBOX_GNUPGHOME) / "S.gpg-agent"
            forwards.append((extra, guest, _free_tcp_port()))
            pubkeys = _export_gpg_pubkeys()
    clipboard_port = _free_tcp_port() if settings.sandbox_clipboard else None
    browser_port: Optional[int] = None
    browser_open_port: Optional[int] = None
    if settings.sandbox_chrome:
        browser_port = _free_tcp_port()
        browser_open_port = _free_tcp_port()
        if not _browser_bridge_live():
            err_console.print(
                "[yellow]Warning: CLAUDE_PROFILE_SANDBOX_CHROME is set but no Claude in "
                "Chrome native host is listening on the host yet. Make sure Chrome is "
                "running with the Claude extension; in the sandbox, run /chrome and pick "
                "'Reconnect extension' to wake it (that opens the connect page in your "
                "host Chrome via the browser-open bridge).[/yellow]"
            )
    return _Forwarding(
        forwards, ssh_auth, pubkeys, clipboard_port, browser_port, browser_open_port
    )


def _start_host_bridge(agent_sock: Path, port: int) -> subprocess.Popen[bytes]:
    """Bridge a host agent socket to a localhost TCP port the VM can reach."""
    return subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork",
            f"UNIX-CONNECT:{agent_sock}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _clipboard_host_handler() -> Path:
    """Path to the packaged host-side clipboard handler script."""
    return Path(str(resources.files("claude_profile") / "clipboard_host.sh"))


def _start_clipboard_host_bridge(port: int) -> subprocess.Popen[bytes]:
    """Serve the host clipboard to the VM: socat execs a read-only wl-paste handler.

    Each guest connection runs the handler with the socket on stdin/stdout; it reads
    one request line (whitelisted wl-paste args) and streams the clipboard bytes
    back. Only clipboard reads cross the boundary — no Wayland access is exposed to
    the sandbox, unlike forwarding the compositor wholesale.
    """
    handler = _clipboard_host_handler()
    return subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork",
            f"EXEC:bash {handler}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _browser_bridge_host_handler() -> Path:
    """Path to the packaged host-side Claude-in-Chrome bridge proxy."""
    return Path(str(resources.files("claude_profile") / "browser_bridge_host.py"))


def _start_browser_host_bridge(port: int) -> subprocess.Popen[bytes]:
    """Serve the host Claude in Chrome native-host socket to the VM.

    socat execs the proxy per guest connection; it resolves the newest
    ``claude --chrome-native-host`` socket (so the bridge follows Chrome's native host
    across restarts, its pid changing each spawn), relays the framed messages, and
    injects a keepalive during idle gaps so Chrome's MV3 service worker does not go idle
    and kill the native host mid-session.
    """
    handler = _browser_bridge_host_handler()
    return subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork",
            f"EXEC:python3 {handler}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _chrome_extension_guest_path() -> Optional[str]:
    """In-VM path to create so claude detects the extension, or None if it isn't installed.

    claude reports "Extension: Installed" by readdir'ing
    ``<chrome-user-data>/<profile>/Extensions/<id>``; the VM has no Chrome install, so it
    always reports "Not detected" even with the bridge working. Find the extension in a
    host browser profile and return the equivalent in-VM path — only the directory's
    existence is checked, so the entrypoint just creates it. Returning None when the
    extension is genuinely absent keeps the reported status honest, and the host's Chrome
    profile (cookies, history, passwords) is never exposed to the VM.
    """
    config = Path.home() / ".config"
    for browser in CHROME_USER_DATA_DIRS:
        user_data = config / browser
        if not user_data.is_dir():
            continue
        for profile in sorted(user_data.iterdir()):
            if not profile.is_dir():
                continue
            if profile.name != "Default" and not profile.name.startswith("Profile "):
                continue
            if (profile / "Extensions" / CHROME_EXTENSION_ID).is_dir():
                return (
                    f"/home/appuser/.config/{browser}/{profile.name}"
                    f"/Extensions/{CHROME_EXTENSION_ID}"
                )
    return None


def _profile_oauth_scopes(profile_dir: Path) -> Optional[list[str]]:
    """Return the profile's OAuth scopes, or None if credentials are absent/unreadable."""
    try:
        data = json.loads((profile_dir / ".credentials.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    scopes = oauth.get("scopes")
    return scopes if isinstance(scopes, list) else None


def _warn_missing_chrome_scope(profile_dir: Path) -> None:
    """Warn when sandbox_chrome is on but the profile's token can't enable Chrome.

    claude checks the OAuth scope first, ahead of ``--chrome`` and every other condition,
    so a profile authenticated with a setup-token (``user:inference`` only) reports
    "Status: Disabled" with no hint as to why. Surface that here instead, since the fix is
    a re-login rather than anything the bridge can do. Unreadable credentials are left
    alone — claude reports auth problems itself.
    """
    scopes = _profile_oauth_scopes(profile_dir)
    if scopes is None or CHROME_OAUTH_SCOPES & set(scopes):
        return
    err_console.print(
        f"[yellow]Warning: CLAUDE_PROFILE_SANDBOX_CHROME is set but profile "
        f"'{profile_dir.name}' has OAuth scopes {sorted(scopes)}, none of which claude "
        f"accepts for Claude in Chrome (needs one of {sorted(CHROME_OAUTH_SCOPES)}). "
        f"Chrome will report 'Disabled' regardless of the bridge. A setup-token login "
        f"grants user:inference only — re-authenticate with a full OAuth login: "
        f"CLAUDE_PROFILE_SANDBOX=0 claude-profile {profile_dir.name} /login[/yellow]"
    )


def _browser_open_host_handler() -> Path:
    """Path to the packaged host-side browser-open handler script."""
    return Path(str(resources.files("claude_profile") / "browser_open_host.sh"))


def _start_browser_open_host_bridge(port: int) -> subprocess.Popen[bytes]:
    """Serve the host browser-open bridge to the VM.

    socat execs the handler per guest connection; the handler opens a Claude connect
    URL (relayed by the in-VM ``google-chrome`` shim) in the host's real Chrome, so the
    sandboxed Claude Code can wake the extension itself. The handler whitelists only
    Anthropic's clau.de/claude.ai chrome URLs.
    """
    handler = _browser_open_host_handler()
    return subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork",
            f"EXEC:bash {handler}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _gh_token() -> Optional[str]:
    """Return the host's GitHub token via ``gh auth token``, or None if unavailable.

    Reads from wherever gh stores it (system keyring or hosts.yml). A short timeout
    avoids hanging if the keyring needs an interactive unlock.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def _with_gh_token(extra_env: dict[str, str]) -> dict[str, str]:
    """Add ``GH_TOKEN`` from the host gh login when ``sandbox_gh`` is enabled.

    gh keeps its token in the keyring or hosts.yml; a microVM can reach neither, so
    we read it on the host and forward it as the GH_TOKEN env var gh reads natively.
    Returns extra_env unchanged when disabled or no token is found.
    """
    if not settings.sandbox_gh:
        return extra_env
    token = _gh_token()
    if not token:
        err_console.print(
            "[yellow]Warning: CLAUDE_PROFILE_SANDBOX_GH is set but no GitHub token "
            "was found (`gh auth token` failed). gh will be unauthenticated in the "
            "sandbox.[/yellow]"
        )
        return extra_env
    return {**extra_env, "GH_TOKEN": token}


@dataclass
class _InfisicalLogin:
    email: str
    domain: str
    token: str
    active: bool


def _jwt_expired(token: str) -> bool:
    """True if a JWT is malformed or past its exp (30s buffer, matching the CLI)."""
    parts = token.split(".")
    if len(parts) != 3:
        return True
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return True
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return True
    return exp <= time.time() + 30


def _infisical_token(email: str) -> Optional[str]:
    """Return the live access token for an infisical login from the OS keyring.

    The CLI stores each login as a JSON ``UserCredentials`` blob under the keyring
    service ``infisical-cli`` keyed by email. Returns the access JWT only when it is
    present and unexpired — the CLI cannot refresh, so an expired token is dead.
    """
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", "infisical-cli", "username", email],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        blob = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    token = blob.get("JTWToken")
    if not isinstance(token, str) or _jwt_expired(token):
        return None
    return token


def _infisical_config() -> dict:
    """Parse the host infisical config, or an empty dict if absent/unreadable."""
    path = Path.home() / ".infisical" / "infisical-config.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _infisical_login_matches(entry: str, user: dict) -> bool:
    """True if an allowlist entry matches a login by email or domain substring."""
    email = (user.get("email") or "").lower()
    domain = (user.get("domain") or "").lower()
    return entry == email or entry in email or entry in domain


def _infisical_logins() -> list[_InfisicalLogin]:
    """Resolve the allowlisted, still-valid infisical logins to forward.

    ``sandbox_infisical`` is a comma-separated allowlist of emails or domain
    substrings. Each entry is matched against the host's logged-in infisical users;
    matches whose keyring token is live are returned. Entries matching nothing, and
    matched logins whose token has expired, are warned about and skipped.
    """
    allow = [
        a.strip().lower() for a in settings.sandbox_infisical.split(",") if a.strip()
    ]
    if not allow:
        return []
    if shutil.which("secret-tool") is None:
        err_console.print(
            "[yellow]Warning: CLAUDE_PROFILE_SANDBOX_INFISICAL is set but 'secret-tool' "
            "is not installed, so infisical tokens can't be read from the keyring. "
            "Install libsecret (provides secret-tool).[/yellow]"
        )
        return []
    config = _infisical_config()
    users = config.get("loggedInUsers") or []
    active_email = config.get("loggedInUserEmail") or ""
    logins: list[_InfisicalLogin] = []
    seen: set[str] = set()
    for entry in allow:
        matches = [u for u in users if _infisical_login_matches(entry, u)]
        if not matches:
            err_console.print(
                f"[yellow]Warning: no logged-in infisical user matches '{entry}' "
                f"(from CLAUDE_PROFILE_SANDBOX_INFISICAL).[/yellow]"
            )
            continue
        for user in matches:
            email = user.get("email", "")
            if not email or email in seen:
                continue
            token = _infisical_token(email)
            if token is None:
                err_console.print(
                    f"[yellow]Warning: infisical login '{email}' has no valid token "
                    f"(expired — run `infisical login` on the host); skipping.[/yellow]"
                )
                continue
            seen.add(email)
            logins.append(
                _InfisicalLogin(
                    email, user.get("domain", ""), token, email == active_email
                )
            )
    return logins


def _with_infisical_env(extra_env: dict[str, str]) -> dict[str, str]:
    """Forward allowlisted infisical logins into the sandbox as env vars.

    infisical keeps login tokens in the OS keyring, which a microVM can't reach, so
    we read them on the host and forward: the primary (the active login if it is
    allowlisted, else the first match) as INFISICAL_TOKEN plus INFISICAL_API_URL/
    INFISICAL_DOMAIN so infisical works with no extra flags, and every allowlisted
    login as CLAUDE_SANDBOX_INFISICAL (JSON) so the agent can target a specific one
    with --token/--domain. The host keyring is left untouched.
    """
    if not settings.sandbox_infisical:
        return extra_env
    logins = _infisical_logins()
    if not logins:
        return extra_env
    primary = next((login for login in logins if login.active), logins[0])
    profiles = [
        {"email": login.email, "domain": login.domain, "token": login.token}
        for login in logins
    ]
    return {
        **extra_env,
        "INFISICAL_TOKEN": primary.token,
        "INFISICAL_API_URL": primary.domain,
        "INFISICAL_DOMAIN": primary.domain,
        "CLAUDE_SANDBOX_INFISICAL": json.dumps(profiles),
    }


def _infisical_briefing(extra_env: dict[str, str]) -> str:
    """System-prompt note describing the forwarded infisical logins, or ''."""
    raw = extra_env.get("CLAUDE_SANDBOX_INFISICAL")
    if not raw:
        return ""
    try:
        profiles = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    listing = ", ".join(f"{p['email']} ({p['domain']})" for p in profiles)
    primary_domain = extra_env.get("INFISICAL_DOMAIN", "")
    return (
        " The infisical CLI is authenticated: INFISICAL_TOKEN and INFISICAL_API_URL "
        f"point at your primary org ({primary_domain}), so `infisical secrets "
        "--projectId … --env …` works as-is. Allowlisted logins (JSON in "
        f"$CLAUDE_SANDBOX_INFISICAL): {listing}. To use a non-primary org, pass its "
        "--token and --domain from that JSON — the env domain overrides any repo "
        ".infisical.json, so always pass --domain for non-primary orgs. Forwarded "
        "tokens expire in ~10 days and the keyring stays on the host, so re-launch to "
        "refresh them."
    )


def _pulumi_token() -> Optional[str]:
    """Return the Pulumi Cloud access token from ~/.pulumi/credentials.json, or None.

    pulumi stores a token per backend; we return the one for the current backend only
    when it is a Pulumi Cloud (https) backend. Self-managed backends (s3://, file://, …)
    carry no token and yield None.
    """
    path = Path.home() / ".pulumi" / "credentials.json"
    try:
        creds = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    current = creds.get("current") or ""
    if not current.startswith("https://"):
        return None
    token = (creds.get("accessTokens") or {}).get(current)
    return token if isinstance(token, str) and token else None


def _with_pulumi_token(extra_env: dict[str, str]) -> dict[str, str]:
    """Add PULUMI_ACCESS_TOKEN from the host Pulumi Cloud login when sandbox_pulumi is set.

    pulumi keeps the token in ~/.pulumi/credentials.json, which a microVM can't reach,
    so we read it on the host and forward it as the env var pulumi reads natively.
    Returns extra_env unchanged when disabled or no token is found.
    """
    if not settings.sandbox_pulumi:
        return extra_env
    token = _pulumi_token()
    if not token:
        err_console.print(
            "[yellow]Warning: CLAUDE_PROFILE_SANDBOX_PULUMI is set but no Pulumi Cloud "
            "token was found in ~/.pulumi/credentials.json. pulumi will be "
            "unauthenticated in the sandbox.[/yellow]"
        )
        return extra_env
    return {**extra_env, "PULUMI_ACCESS_TOKEN": token}


def _with_forwarded_env(extra_env: dict[str, str]) -> dict[str, str]:
    """Forward named host env vars into the sandbox (sandbox_forward_env).

    A comma-separated list of variable names; each one present in the host environment
    is copied into the VM. Lets host-oriented MCP servers that pass secrets through as
    ``-e VAR`` (e.g. BUILDKITE_API_TOKEN, GITHUB_PERSONAL_ACCESS_TOKEN) find them inside
    the VM. A name the profile's ``.env`` already supplies (``claude-profile env --set``)
    is left alone — it reaches the VM either way, so warning about it would be wrong. Only
    names available from neither source are warned about and skipped.
    """
    names = [n.strip() for n in settings.sandbox_forward_env.split(",") if n.strip()]
    if not names:
        return extra_env
    forwarded = dict(extra_env)
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            forwarded[name] = value
            continue
        if name in extra_env:
            continue  # already provided by the profile's .env
        err_console.print(
            f"[yellow]Warning: CLAUDE_PROFILE_SANDBOX_FORWARD_ENV lists '{name}' but it "
            f"is set neither in the environment nor in the profile's .env; skipping. Set "
            f"it with: claude-profile env <name> --set {name}=…[/yellow]"
        )
    return forwarded


def _launch_sandbox(
    profile_dir: Path, claude_args: list[str], extra_env: dict[str, str]
) -> None:
    """Launch a podman krun microVM running claude (optionally bridging agents)."""
    _ensure_sandbox_image()
    image_user = _sandbox_image_user()
    if image_user and image_user not in ("root", "0"):
        err_console.print(
            f"[yellow]Warning: sandbox image '{settings.sandbox_image}' runs as "
            f"'{image_user}', not root — the entrypoint must start as root to map your "
            f"UID and forward SSH/GPG agents. End your Containerfile with `USER root`."
            f"[/yellow]"
        )
    if settings.sandbox_chrome:
        _warn_missing_chrome_scope(profile_dir)
        ext_path = _chrome_extension_guest_path()
        if ext_path is not None:
            extra_env = {**extra_env, SANDBOX_CHROME_EXT_PATH_ENV: ext_path}
    extra_env = _with_gh_token(extra_env)
    extra_env = _with_infisical_env(extra_env)
    extra_env = _with_pulumi_token(extra_env)
    extra_env = _with_forwarded_env(extra_env)
    cwd = Path.cwd()
    forwarding = _build_forwarding()
    if forwarding.active():
        _run_sandbox_supervised(profile_dir, cwd, claude_args, extra_env, forwarding)
        return
    argv = _build_sandbox_argv(profile_dir, cwd, claude_args, extra_env)
    os.execvpe(settings.podman_bin, argv, os.environ.copy())


def _run_sandbox_supervised(
    profile_dir: Path,
    cwd: Path,
    claude_args: list[str],
    extra_env: dict[str, str],
    forwarding: _Forwarding,
) -> None:
    """Run the VM as a child so the host agent bridges are torn down on exit.

    The exec model cannot manage the socat bridges' lifetime, so agent-forwarding
    mode supervises podman instead. podman keeps the terminal in raw mode, so the
    TUI behaves the same as the exec path.
    """
    if shutil.which("socat") is None:
        err_console.print(
            "[red]'socat' not found on host.[/red] Install it (e.g. dnf install socat) "
            "or disable the sandbox agent-forwarding settings."
        )
        sys.exit(1)
    if forwarding.clipboard_port is not None and shutil.which("wl-paste") is None:
        err_console.print(
            "[red]'wl-paste' not found on host.[/red] Install wl-clipboard (e.g. "
            "dnf install wl-clipboard) or unset CLAUDE_PROFILE_SANDBOX_CLIPBOARD."
        )
        sys.exit(1)
    bridges = [
        _start_host_bridge(host, port) for host, _guest, port in forwarding.forwards
    ]
    if forwarding.clipboard_port is not None:
        bridges.append(_start_clipboard_host_bridge(forwarding.clipboard_port))
    if forwarding.browser_port is not None:
        bridges.append(_start_browser_host_bridge(forwarding.browser_port))
    if forwarding.browser_open_port is not None:
        bridges.append(_start_browser_open_host_bridge(forwarding.browser_open_port))
    argv = _build_sandbox_argv(profile_dir, cwd, claude_args, extra_env, forwarding)
    try:
        result = subprocess.run(argv, env=os.environ.copy())
    finally:
        for bridge in bridges:
            bridge.terminate()
    sys.exit(result.returncode)


def _sandbox_enabled(profile_dir: Path) -> bool:
    """Decide whether to launch in a sandbox.

    The ``CLAUDE_PROFILE_SANDBOX`` env override wins when set; otherwise the
    profile's ``.sandbox`` marker decides.
    """
    if settings.sandbox is not None:
        return settings.sandbox
    return (profile_dir / SANDBOX_MARKER).exists()


def _launch_profile(name: str, claude_args: list[str]) -> None:
    d = settings.profiles_base / name
    if not d.exists():
        err_console.print(
            f"[red]Profile '{name}' does not exist.[/red] "
            f"Create it with: claude-profile add {name}"
        )
        sys.exit(1)
    env_file = d / ".env"
    extra_env = _parse_env_file(env_file) if env_file.exists() else {}
    if _sandbox_enabled(d):
        _launch_sandbox(d, claude_args, extra_env)
        return
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(d)
    env.update(extra_env)
    # exec replaces this process - no wrapper in between, which matters for
    # claude's TUI (raw terminal mode, signal handling, etc.)
    os.execvpe(settings.claude_bin, [settings.claude_bin] + claude_args, env)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] not in KNOWN_COMMANDS and not args[0].startswith("-"):
        _launch_profile(args[0], args[1:])
    else:
        app()
