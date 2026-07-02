"""claude-profile - Launch Claude Code with an isolated config directory."""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import sys
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
# podman's default host.containers.internal address under pasta; mapping it to the
# host loopback lets the agent bridge bind to 127.0.0.1 instead of all interfaces.
SANDBOX_HOST_LOOPBACK = "169.254.1.2"
SANDBOX_BRIEFING = (
    "You are running inside the claude-profile microVM sandbox — an ephemeral "
    "podman/krun VM (confirm with /run/.containerenv). Only the mounted working "
    "directory and its git dir, plus this profile's Claude config, are visible; the "
    "rest of the host filesystem is not, which is why --dangerously-skip-permissions is "
    "safe here. Anything you install is discarded when the session ends, and you have "
    "passwordless sudo scoped to dnf. To add a missing tool use `sudo dnf install <pkg>` "
    "or `uv tool install <tool>` (see the sandbox-tools skill). If you need a tool made "
    "permanent, access outside the mounted paths, or anything the sandbox blocks, ask "
    "the user instead of working around it."
)
# Curated dev tools advertised by the sandbox-tools skill (command name -> description).
SANDBOX_SKILL_TOOLS: dict[str, str] = {
    "uv": "Python package/tool manager (uv tool install, uv run)",
    "python3": "Python 3 (with pyyaml and jinja2)",
    "jq": "JSON processor",
    "yq": "YAML processor",
    "git": "Git",
    "rg": "ripgrep (fast search)",
    "fd": "fd (fast file finder)",
    "make": "make",
    "gcc": "C compiler",
    "openssl": "OpenSSL",
    "trash": "trash-cli (use instead of rm -rf)",
    "ssh": "OpenSSH client",
    "gpg": "GnuPG",
    "socat": "socat",
    "node": "Node.js",
    "npm": "npm",
    "butane": "Butane (Ignition config compiler)",
}

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
    git_dir = _git_common_dir(cwd)
    if git_dir is not None and git_dir != cwd and cwd not in git_dir.parents:
        mounts += ["-v", f"{git_dir}:{git_dir}:z"]
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        mounts += ["-v", f"{gitconfig}:/home/appuser/.gitconfig:ro,z"]
    mounts += _linked_dir_mounts(profile_dir)
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
        "--userns=keep-id",
        "--device",
        "/dev/kvm",
    ]
    if forwarding and forwarding.forwards:
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
    argv += _forwarding_env(forwarding)
    for key, value in extra_env.items():
        argv += ["-e", f"{key}={value}"]
    argv += _sandbox_mounts(profile_dir, cwd)
    argv += ["-w", str(cwd), settings.sandbox_image, "claude"]
    args = list(claude_args)
    if settings.sandbox_skip_permissions and SKIP_PERMISSIONS_FLAG not in args:
        args.append(SKIP_PERMISSIONS_FLAG)
    if "--append-system-prompt" not in args:
        args += ["--append-system-prompt", SANDBOX_BRIEFING]
    return argv + args


def _forwarding_env(forwarding: Optional[_Forwarding]) -> list[str]:
    """Env args telling the entrypoint which sockets to bridge and how."""
    if forwarding is None or not forwarding.forwards:
        return []
    spec = ",".join(f"{guest}={port}" for _host, guest, port in forwarding.forwards)
    env = ["-e", f"CLAUDE_SANDBOX_FORWARDS={spec}"]
    if forwarding.ssh_auth_sock is not None:
        env += ["-e", f"SSH_AUTH_SOCK={forwarding.ssh_auth_sock}"]
    if forwarding.gpg_pubkeys_b64 is not None:
        env += [
            "-e",
            f"GNUPGHOME={SANDBOX_GNUPGHOME}",
            "-e",
            f"CLAUDE_SANDBOX_GPG_PUBKEYS={forwarding.gpg_pubkeys_b64}",
        ]
    return env


def _free_tcp_port() -> int:
    """Return an unused localhost TCP port for an agent bridge."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ssh_agent_sockets() -> list[Path]:
    """Host SSH agent sockets to bridge: the active agent plus 1Password."""
    sockets: list[Path] = []
    auth = os.environ.get("SSH_AUTH_SOCK")
    if auth and Path(auth).exists():
        sockets.append(Path(auth))
    onepassword = Path.home() / ".1password" / "agent.sock"
    if onepassword.exists() and onepassword not in sockets:
        sockets.append(onepassword)
    return sockets


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
    return _Forwarding(forwards, ssh_auth, pubkeys)


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
    cwd = Path.cwd()
    forwarding = _build_forwarding()
    if forwarding.forwards:
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
    bridges = [
        _start_host_bridge(host, port) for host, _guest, port in forwarding.forwards
    ]
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
    _KNOWN_COMMANDS = {
        "list",
        "add",
        "remove",
        "links",
        "env",
        "build",
        "sandbox",
        "sandbox-skill",
    }
    args = sys.argv[1:]
    if args and args[0] not in _KNOWN_COMMANDS and not args[0].startswith("-"):
        _launch_profile(args[0], args[1:])
    else:
        app()
