"""claude-profile - Launch Claude Code with an isolated config directory."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import KW_ONLY, dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import typer
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from claude_profile import bridges


class Settings(BaseSettings):
    # An empty value means unset: `CLAUDE_PROFILE_SANDBOX=` clears an exported override
    # instead of failing every command on a bool that cannot be parsed.
    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_PROFILE_", env_ignore_empty=True
    )

    profiles_base: Path = Field(
        default_factory=lambda: Path.home() / ".claude-profiles"
    )
    claude_bin: str = Field(default="claude")
    podman_bin: str = Field(default="podman")
    infisical_bin: str = Field(default="infisical")
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
    # emails or domains (e.g. "corp.example,quickvm.com"). Empty = disabled.
    sandbox_infisical: str = Field(default="")
    sandbox_pulumi: bool = Field(default=False)
    # Comma-separated names of host env vars to copy into the sandbox (e.g. tokens that
    # host-oriented MCP servers pass through as `-e VAR`). Empty = none forwarded.
    sandbox_forward_env: str = Field(default="")
    # Per-launch override of the .sandbox marker (CLAUDE_PROFILE_SANDBOX). None = use marker.
    sandbox: bool | None = Field(default=None)


settings = Settings()
console = Console()
err_console = Console(stderr=True)

# hooks: a hook or statusLine command written as ~/.claude/hooks/... resolves to the
# profile dir inside the sandbox, so a guard hook there only runs if the link does.
LINKABLE_DIRS: tuple[str, ...] = ("commands", "skills", "hooks")
# Symlinked into each profile rather than copied: the sandbox reaches it through the
# profile dir, and a copy goes stale the moment the global script changes.
STATUSLINE_FILE = "statusline.sh"
SANDBOX_MARKER = ".sandbox"
SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
SANDBOX_CONFIG_DIR = "/home/appuser/.claude"
SANDBOX_GNUPGHOME = "/home/appuser/.gnupg"
# In-VM dir for the forwarded SSH agent sockets. Not the host's own path: the entrypoint
# chowns each socket's parent, and when that is /run/user/<uid> gpg moves its socket
# dir there and never reaches the GPG bridge in GNUPGHOME.
SANDBOX_AGENT_DIR = "/run/claude-sandbox"
# In-VM path of the host's public keyring, which the entrypoint imports into GNUPGHOME.
SANDBOX_GPG_PUBKEYS = "/opt/claude-host/gpg-pubkeys"
# In-VM path where the host's Claude Code binary is mounted read-only. The entrypoint
# points the PATH entry at it, so the VM runs the host's version instead of the one
# baked into the image (see _sandbox_claude_binary).
SANDBOX_HOST_CLAUDE = "/opt/claude-host/claude"
# In-VM path where the shared, read-only MCP image store is mounted (additionalimagestore).
SANDBOX_IMAGE_STORE = "/var/lib/shared-mcp-store"
# ioctl that makes a file share another's blocks (a reflink), from linux/fs.h.
FICLONE = 0x40049409
# Trust anchors, and the bundles update-ca-trust extracts from them and the system roots.
# Same paths on host and in the VM: the host's are mounted over the image's (see
# _ca_trust_mounts).
SANDBOX_CA_ANCHORS = "/etc/pki/ca-trust/source/anchors"
SANDBOX_CA_EXTRACTED = "/etc/pki/ca-trust/extracted"
# The bridge server's port and this launch's token, as the in-VM side sees them. The
# token travels in the secret env file (see _secret_env_file), never on podman's argv.
SANDBOX_BRIDGE_PORT_ENV = "CLAUDE_SANDBOX_BRIDGE_PORT"
SANDBOX_BRIDGE_TOKEN_ENV = "CLAUDE_SANDBOX_BRIDGE_TOKEN"
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
# Infisical Agent Vault (see _route_agent_vault). A profile opts in with these keys in its
# .env, which the VM can't change; claude-profile reads them and claude never sees them.
AGENT_VAULT_PREFIX = "CLAUDE_PROFILE_AGENT_VAULT_"
AGENT_VAULT_REQUIRED: tuple[str, ...] = ("BUNDLE", "PROXY", "CA_FINGERPRINT")
AGENT_VAULT_OPTIONAL: tuple[str, ...] = ("INFISICAL_PROFILE", "NO_PROXY")
AGENT_VAULT_FINGERPRINT = re.compile(r"SHA256(:[0-9A-F]{2}){32}")
# Set when a launch execs `infisical agent-vault run`, to the profile's name: the launch
# the CLI starts finds it and goes on to claude instead of wrapping itself again.
AGENT_VAULT_RUN_ENV = "CLAUDE_PROFILE_AGENT_VAULT_RUN"
# Tells the VM whether its credentials are brokered: "active" or "unavailable".
AGENT_VAULT_STATE_ENV = "CLAUDE_SANDBOX_AGENT_VAULT"
AGENT_VAULT_SESSION_TTL = "24h"
AGENT_VAULT_PREFLIGHT_TIMEOUT = 5.0
# claude's own traffic skips the proxy: the Claude API, the OAuth hosts (claude 2.1.296
# refreshes its token at platform.claude.com) and the Claude in Chrome bridge. Through it,
# the proxy would decrypt every prompt and the profile's OAuth tokens, and a proxy outage
# would take claude down too. Both forms of each apex domain, because clients disagree:
# proxy-from-env (axios) matches `claude.ai` exactly, and Go's `.claude.ai` skips the apex.
AGENT_VAULT_NO_PROXY: tuple[str, ...] = (
    ".anthropic.com",
    "claude.ai",
    ".claude.ai",
    "claude.com",
    ".claude.com",
    ".claudeusercontent.com",
)
# What `infisical agent-vault run` sets for its child. The proxy URLs (and the CLI's copy
# for OpenClaw) carry the session token; the CA variables name a file holding only the
# proxy's CA.
AGENT_VAULT_PROXY_URL_VARS: tuple[str, ...] = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "https_proxy",
    "http_proxy",
)
AGENT_VAULT_SESSION_VARS: tuple[str, ...] = (
    *AGENT_VAULT_PROXY_URL_VARS,
    "OPENCLAW_PROXY_URL",
)
AGENT_VAULT_PROXY_VARS: tuple[str, ...] = ("NO_PROXY", "no_proxy", "NODE_USE_ENV_PROXY")
AGENT_VAULT_CA_VARS: tuple[str, ...] = (
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "DENO_CERT",
)
# The VM's system bundle, which /etc/ssl/cert.pem and the image's other default paths
# link to. A vault launch mounts the combined bundle over it.
SANDBOX_VM_CA_BUNDLE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
AGENT_VAULT_BRIEFING = (
    " Credentials are brokered: your HTTP(S) traffic goes through an Infisical Agent Vault "
    "proxy, which attaches the real credential to requests for the services in this "
    "session's access bundle. Token variables such as GH_TOKEN hold placeholders, not "
    "secrets, and tools authenticate with them as they are. HTTPS_PROXY and HTTP_PROXY "
    "carry this session's token: never print, log or copy them."
)
AGENT_VAULT_UNAVAILABLE_BRIEFING = (
    " Agent Vault was unavailable when this session started, so no credentials are "
    "brokered: requests to services that need one will fail to authenticate. Tell the user "
    "instead of looking for credentials elsewhere."
)
# Git settings resolved on the host for the CWD and given to the VM (see
# _git_identity_mounts). Signing settings go only when GPG is forwarded.
GIT_IDENTITY_KEYS: tuple[str, ...] = ("user.name", "user.email")
GIT_SIGNING_KEYS: tuple[str, ...] = ("user.signingkey", "commit.gpgsign", "gpg.format")
# In-VM paths: the mounted ~/.gitconfig, and the generated global config that includes it.
SANDBOX_GITCONFIG = "/home/appuser/.gitconfig"
SANDBOX_GIT_IDENTITY = "/home/appuser/.gitconfig-identity"
# Settings every profile gets (e.g. hooks), in the profiles base and passed to each launch
# with --settings; "{profile}" in any string becomes the profile name.
SHARED_SETTINGS = "shared-settings.json"
PROFILE_PLACEHOLDER = "{profile}"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
# Names `env --set` accepts, and names a profile can have (see _check_profile_name).
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SANDBOX_BRIEFING = (
    "You are running inside the claude-profile microVM sandbox — an ephemeral "
    "podman/krun VM (confirm with /run/.containerenv). The host filesystem is visible "
    "only where it is mounted: the working directory and its git dir, this profile's "
    "Claude config, and a few read-only files. Whatever you change under those mounts "
    "persists on the host, so treat those changes as you would on the host. The files "
    "the host runs commands from are protected instead: this repo's .git/config is "
    "read-only, so git commands that save settings to it (push -u, branch -u, remote "
    "add, git config) cannot save them, though the push itself still works (prefer "
    "`git push origin HEAD`); .git/hooks, this profile's settings.json and "
    ".claude/settings.local.json are copies, discarded when the session ends along with "
    "anything you install. You have passwordless sudo for dnf and podman. To add a "
    "missing tool use `sudo dnf "
    "install <pkg>` or `uv tool install <tool>` (see the sandbox-tools skill). Nested "
    "containers run rootful automatically — just use `podman` (it is wrapped to sudo "
    "because rootless can't unpack layers in the VM's user namespace); give containers that "
    'write into the mounted repo --user "$(id -u):$(id -g)", or the host user cannot delete '
    "what they write. If you need a tool made "
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
        sandbox = "✓ microVM" if _sandbox_marked(p) else "—"
        table.add_row(p.name, status, sandbox)

    console.print(table)
    note = _sandbox_override_note()
    if note is not None:
        console.print(note)


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
    _check_profile_name(name)
    d = settings.profiles_base / name
    if d.exists():
        err_console.print(f"[yellow]Profile '{name}' already exists at {d}[/yellow]")
        raise typer.Exit(code=1)
    d.mkdir(parents=True)
    _seed_from_global(d)

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


def _check_profile_name(name: str) -> None:
    """Exit unless name can be launched as `claude-profile <name>`.

    A subcommand's name would run that command instead, a path-like name would land
    outside the profiles dir, and shared-settings.json is the shared settings file.
    """
    if (
        not PROFILE_NAME.fullmatch(name)
        or name in KNOWN_COMMANDS
        or name == SHARED_SETTINGS
    ):
        err_console.print(
            f"[red]Error: '{name}' can't be a profile name. Use letters, digits, '.', '_' "
            f"and '-', starting with a letter or digit, and not a claude-profile "
            f"command.[/red]"
        )
        raise typer.Exit(code=1)


def _seed_from_global(profile_dir: Path) -> None:
    """Copy settings.json and CLAUDE.md from ~/.claude into a new profile, and link
    its statusline.sh to the global one (see STATUSLINE_FILE)."""
    claude_dir = Path.home() / ".claude"
    for fname in ("settings.json", "CLAUDE.md"):
        src = claude_dir / fname
        if src.exists():
            shutil.copy2(src, profile_dir / fname)
    statusline = claude_dir / STATUSLINE_FILE
    if statusline.exists():
        (profile_dir / STATUSLINE_FILE).symlink_to(statusline)


def _sandbox_data_dir() -> Path:
    """Return the packaged sandbox build context (Containerfile + entrypoint.sh)."""
    return Path(str(resources.files("claude_profile") / "sandbox"))


def _sandbox_image_exists() -> bool:
    """Return True if the configured sandbox image is present locally."""
    try:
        result = subprocess.run(
            [settings.podman_bin, "image", "exists", settings.sandbox_image],
            capture_output=True,
            check=False,
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
            check=False,
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
    # exit 0: the loop's status is its last `command -v`, so a missing last tool would
    # otherwise look like a failed run.
    script = f'for t in {names}; do command -v "$t" >/dev/null 2>&1 && echo "$t"; done; exit 0'
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
        check=False,
    )
    if result.returncode != 0:
        # Otherwise the skill would be rewritten listing no tools at all.
        err_console.print(
            f"[red]Could not list the tools in {settings.sandbox_image} (podman exit "
            f"{result.returncode}): {result.stderr.strip()}[/red]"
        )
        sys.exit(1)
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
    path: Path | None = typer.Option(
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


# podman/docker run options that take the next arg as their value (see
# _image_ref_from_args).
RUN_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "-e",
        "--env",
        "--env-file",
        "-v",
        "--volume",
        "--mount",
        "-w",
        "--workdir",
        "--name",
        "--network",
        "--entrypoint",
        "-p",
        "--publish",
        "-l",
        "--label",
        "-u",
        "--user",
    }
)


def _image_ref_from_args(args: list) -> str | None:
    """Pick the container image ref out of a podman/docker ``run`` arg list.

    The image is the first arg that is not a flag, a flag's value or a path, and whose
    first path segment looks like a registry host (has a ``.`` or ``:``), as in a
    fully-qualified ref like ``ghcr.io/o/i:tag``. Values of RUN_VALUE_FLAGS are skipped
    because they can look the same (``-e URL=https://h/x``, ``-v cache:/data``).
    """
    skip_value = False
    for arg in args:
        if skip_value or not isinstance(arg, str):
            skip_value = False
            continue
        if arg in RUN_VALUE_FLAGS:
            skip_value = True
            continue
        if arg.startswith(("-", "/")) or "/" not in arg:
            continue
        host = arg.split("/", 1)[0]
        if "." in host or ":" in host:
            return arg
    return None


def _mcp_server_blocks(config: Path) -> list[Any]:
    """Return every ``mcpServers`` block in one MCP config file.

    Covers both the user-scope block and the per-project ones ``.claude.json`` nests
    under ``projects``. An unreadable or malformed file yields nothing: a broken config
    should not abort a launch that has other places to look.
    """
    try:
        data = json.loads(config.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    blocks = [data.get("mcpServers")]
    projects = data.get("projects")
    if isinstance(projects, dict):
        blocks += [
            p.get("mcpServers") for p in projects.values() if isinstance(p, dict)
        ]
    return blocks


def _mcp_container_images(profile_dir: Path, cwd: Path) -> list[str]:
    """Return the image refs used by the profile's podman/docker MCP servers.

    Reads the profile's ``.claude.json`` plus the ``.mcp.json`` files claude picks up
    from directories above ``cwd``, which the sandbox mounts and so can run too.
    Extracts the image from each stdio server run via podman/docker. De-duplicated.
    """
    blocks: list[Any] = []
    for config in [profile_dir / ".claude.json", *_ancestor_mcp_json(cwd)]:
        blocks += _mcp_server_blocks(config)
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
    if clear:
        if store.exists():
            # Delete just the store (its files belong to subuids, hence unshare).
            # `podman system reset` would also stop the user's rootless pause process
            # and wipe the run root their other rootless containers share.
            _run_checked([settings.podman_bin, "unshare", "rm", "-rf", str(store)])
        console.print(f"[green]Image cache cleared ({store}).[/green]")
        return
    fuse = shutil.which("fuse-overlayfs")
    if fuse is None:
        err_console.print(
            "[red]'fuse-overlayfs' not found on host.[/red] Install it "
            "(dnf install fuse-overlayfs)."
        )
        raise typer.Exit(code=1)
    profile_dir = settings.profiles_base / name
    if not profile_dir.exists():
        err_console.print(f"[red]Profile '{name}' does not exist.[/red]")
        raise typer.Exit(code=1)
    images = _mcp_container_images(profile_dir, Path.cwd())
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

    if not on and not off:
        _print_sandbox_status(name, d)
        return

    marker = d / SANDBOX_MARKER
    if on:
        # touch() would follow a linked marker and create its target instead.
        if marker.is_symlink():
            marker.unlink()
        marker.touch()
        console.print(f"[green]Sandbox enabled for '{name}'.[/green]")
        if not _sandbox_image_exists():
            console.print("Build the image first: claude-profile build")
    else:
        marker.unlink(missing_ok=True)
        console.print(f"[green]Sandbox disabled for '{name}' (runs on host).[/green]")
    note = _sandbox_override_note()
    if note is not None and settings.sandbox != on:
        console.print(f"[yellow]{note}[/yellow]")


def _print_sandbox_status(name: str, profile_dir: Path) -> None:
    """Say where the profile launches, and why when CLAUDE_PROFILE_SANDBOX decides it."""
    state = "microVM" if _sandbox_enabled(profile_dir) else "host"
    console.print(f"Profile '{name}' launches on: {state}")
    note = _sandbox_override_note()
    if note is not None:
        own = "microVM" if _sandbox_marked(profile_dir) else "host"
        console.print(f"{note} Its own setting is {own}.")


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
    dir_name: str | None = typer.Argument(
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
        _link_dirs(d, dirs, named=dir_name is not None)
    else:
        for dn in dirs:
            _do_unlink(d, dn)


def _link_dirs(profile_dir: Path, dirs: tuple[str, ...], *, named: bool) -> None:
    """Link each dir to its ~/.claude counterpart.

    Linking every dir skips the ones with no global counterpart (most people have no
    ~/.claude/hooks); a dir named on the command line must exist.
    """
    for dn in dirs:
        if not named and not (Path.home() / ".claude" / dn).exists():
            console.print(f"Skipping '{dn}': ~/.claude/{dn} does not exist.")
            continue
        _do_link(profile_dir, dn)


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
    set_var: list[str] | None = typer.Option(
        None, "--set", help="Set a variable: KEY=VALUE"
    ),
    unset_var: list[str] | None = typer.Option(
        None, "--unset", help="Unset a variable by name"
    ),
) -> None:
    """Manage per-profile environment variables stored in .env."""
    d = settings.profiles_base / name
    if not d.exists():
        err_console.print(f"[red]Profile '{name}' does not exist.[/red]")
        raise typer.Exit(code=1)

    env_file = d / ".env"
    existing = _load_profile_env(d)

    if not set_var and not unset_var:
        _show_env_table(name, existing)
        return

    _apply_env_changes(existing, set_var or [], unset_var or [])
    _write_env_file(env_file, existing)
    console.print(f"[green]Updated .env for profile '{name}'.[/green]")


def _apply_env_changes(
    env_vars: dict[str, str], set_var: list[str], unset_var: list[str]
) -> None:
    """Apply ``--set KEY=VALUE`` and ``--unset KEY`` entries to env_vars in place."""
    for entry in set_var:
        if "=" not in entry:
            err_console.print(
                f"[red]Error: '{entry}' is not valid. Use KEY=VALUE.[/red]"
            )
            raise typer.Exit(code=1)
        key, _, value = entry.partition("=")
        key = key.strip()
        if not ENV_NAME.fullmatch(key):
            err_console.print(
                f"[red]Error: '{key}' is not a variable name (letters, digits and _, not "
                f"starting with a digit).[/red]"
            )
            raise typer.Exit(code=1)
        env_vars[key] = value.strip()
    for key in unset_var:
        if key not in env_vars:
            err_console.print(f"[yellow]Warning: '{key}' is not set.[/yellow]")
            continue
        del env_vars[key]


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
    """Write .env readable by the user only: it holds tokens, and the profile dir is not
    private (it is world-readable by default, like ~/.claude). An existing file is
    tightened to 0600 before the new contents go in."""
    lines = [f"{key}={value}" for key, value in sorted(env_vars.items())]
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with open(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n" if lines else "")


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


def _load_profile_env(profile_dir: Path) -> dict[str, str]:
    """Parse the profile's .env, refusing one that is a symlink.

    The profile dir is writable from inside the sandbox. A .env planted there as a link
    (to ~/.aws/credentials, say) would feed that file's KEY=VALUE lines into the next VM,
    and `env --set` would overwrite the link's target. claude-profile only ever writes a
    regular file.
    """
    env_file = profile_dir / ".env"
    if env_file.is_symlink():
        err_console.print(
            f"[red]Refusing to use {env_file}: it is a symlink (to "
            f"{os.readlink(env_file)}), not a file claude-profile wrote. Check it and "
            f"remove it before launching this profile.[/red]"
        )
        sys.exit(1)
    return _parse_env_file(env_file) if env_file.exists() else {}


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


def _git_toplevel(cwd: Path) -> Path | None:
    """Root of the git work tree containing cwd, or None outside one."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    out = result.stdout.strip()
    return Path(out).resolve() if result.returncode == 0 and out else None


def _sandbox_work_root(cwd: Path) -> Path:
    """The directory mounted read-write for cwd: its git work tree's root, if any.

    Mounting only a repo subdirectory while the repo's .git is mounted read-write
    showed git every other tracked file as deleted, and `git commit -a` in the VM
    recorded those deletions in the host repo.
    """
    return _git_toplevel(cwd) or cwd


def _git_common_dir(cwd: Path) -> Path | None:
    """Return the absolute git common dir for cwd, or None if not in a repo.

    For a worktree this is the main repo's .git dir, which lives outside the
    worktree and must be mounted so git works inside the microVM.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        return None
    common = Path(out)
    return common.resolve() if common.is_absolute() else (cwd / common).resolve()


def _sandbox_state_dir(profile_dir: Path) -> Path:
    """Host-only dir for the files the launcher generates for a profile's sandbox.

    The profile dir is mounted read-write into the VM, so a generated file kept there
    can be swapped for a symlink by the agent, and the next launch would write through
    it or mount its target (~/.bashrc as known_hosts, ~/.ssh as the chrome dir). Files
    in this dir are bind-mounted individually: the VM can change their contents but
    cannot replace them.
    """
    state = _sandbox_state_path(profile_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    return state


def _sandbox_state_path(profile_dir: Path) -> Path:
    """Where _sandbox_state_dir keeps a profile's files; it exists once a launch has."""
    return _data_dir() / "profiles" / profile_dir.name


def _sandbox_settings_overlay(profile_dir: Path) -> Path:
    """Write the VM's per-launch copy of the profile's settings.json; return its path.

    claude on the host runs the hooks and statusline command in settings.json, and the
    VM sees the profile dir read-write, so the VM gets this copy mounted over the file
    instead: settings it changes last the session and never reach the host. The copy
    drops the host-oriented deny rules matching SANDBOX_STRIP_DENY_PREFIXES (e.g.
    ``Bash(sudo *)``): deny wins even under --dangerously-skip-permissions, and the VM
    grants scoped sudo. A missing settings.json is created as ``{}`` so there is a file
    to mount over, which podman would otherwise create empty on the host. A linked one is
    never read through: the VM could have aimed the link at another profile's
    credentials to get them copied into the next sandbox.
    """
    src = profile_dir / "settings.json"
    if src.is_symlink():
        err_console.print(
            f"[yellow]Warning: {src} is a symlink, so the sandbox gets empty settings "
            f"instead of reading through it. Replace it with a regular file to use "
            f"these settings in the sandbox.[/yellow]"
        )
        content = b"{}\n"
    else:
        if not src.exists():
            src.write_text("{}\n")
        content = _sandbox_settings_content(src)
    overlay = _sandbox_state_dir(profile_dir) / "settings.json"
    try:
        overlay.write_bytes(content)
    except OSError as exc:
        err_console.print(
            f"[red]Could not write the sandbox's copy of settings.json to {overlay} "
            f"({exc}).[/red] Without it the VM would change the profile's real settings."
        )
        sys.exit(1)
    return overlay


def _sandbox_settings_content(src: Path) -> bytes:
    """The profile's settings.json as the VM gets it, the host's sudo denies dropped."""
    try:
        raw = src.read_bytes()
    except OSError as exc:
        err_console.print(f"[red]Could not read {src} ({exc}).[/red]")
        sys.exit(1)
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if not _strip_sandbox_denies(data):
        return raw
    return json.dumps(data, indent=2).encode()


def _strip_sandbox_denies(data: object) -> bool:
    """Drop the host-oriented denies in SANDBOX_STRIP_DENY_PREFIXES from a settings object.

    deny wins even under --dangerously-skip-permissions, so a host's ``Bash(sudo *)``
    would block the VM's own scoped sudo. Returns whether any rule was dropped.
    """
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict) or not isinstance(perms.get("deny"), list):
        return False
    kept = [
        rule
        for rule in perms["deny"]
        if not (isinstance(rule, str) and rule.startswith(SANDBOX_STRIP_DENY_PREFIXES))
    ]
    if len(kept) == len(perms["deny"]):
        return False
    perms["deny"] = kept
    return True


def _has_option(args: list[str], name: str) -> bool:
    """True if args carry the option ``name``, as ``name value`` or ``name=value``."""
    return any(arg == name or arg.startswith(f"{name}=") for arg in args)


def _shared_settings_args(
    name: str, claude_args: list[str], *, sandbox: bool
) -> tuple[list[str], bool]:
    """``--settings`` args for the shared settings file, and whether the VM needs host loopback.

    ``shared-settings.json`` in the profiles base holds settings every profile gets, such
    as hooks. claude merges ``--settings`` over the profile's own settings.json, and hook
    entries from both run. ``{profile}`` in any string becomes the profile name. In the
    sandbox, HTTP hooks aimed at the host's loopback are re-pointed so they still reach it,
    and host-oriented denies are dropped as from the profile's settings.json.
    """
    path = settings.profiles_base / SHARED_SETTINGS
    if not path.exists():
        return [], False
    if _has_option(claude_args, "--settings"):
        err_console.print(
            f"[yellow]--settings was given, so {path} is not applied.[/yellow]"
        )
        return [], False
    try:
        data = _fill_profile(json.loads(path.read_text()), name)
    except (OSError, json.JSONDecodeError) as err:
        err_console.print(f"[red]Can't read {path}: {err}[/red]")
        sys.exit(1)
    if not isinstance(data, dict):
        err_console.print(f"[red]{path} must hold a JSON object.[/red]")
        sys.exit(1)
    host_loopback = sandbox and _rewrite_loopback_hooks(data)
    if sandbox:
        _strip_sandbox_denies(data)
    return ["--settings", json.dumps(data)], host_loopback


def _fill_profile(value: Any, name: str) -> Any:
    """Replace ``{profile}`` with the profile name in every string inside ``value``."""
    if isinstance(value, str):
        return value.replace(PROFILE_PLACEHOLDER, name)
    if isinstance(value, list):
        return [_fill_profile(item, name) for item in value]
    if isinstance(value, dict):
        return {key: _fill_profile(item, name) for key, item in value.items()}
    return value


def _rewrite_loopback_hooks(data: dict[str, Any]) -> bool:
    """Point HTTP hooks aimed at the host's loopback at the VM's route to it.

    Inside the microVM 127.0.0.1 is the VM itself; under --map-host-loopback the host's
    loopback answers at SANDBOX_HOST_LOOPBACK instead. Returns whether any hook moved, so
    the caller knows the VM needs that mapping even without an agent bridge.
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    rewrote = False
    for groups in hooks.values():
        for handler in _http_hook_handlers(groups):
            url = urlsplit(handler["url"])
            # Swap only the host, keeping any user:password@ and the port as written
            # (url.port would raise on a malformed port and abort the launch).
            userinfo, at, hostport = url.netloc.rpartition("@")
            host, colon, port = hostport.partition(":")
            if host.lower() in LOOPBACK_HOSTS:
                netloc = f"{userinfo}{at}{SANDBOX_HOST_LOOPBACK}{colon}{port}"
                handler["url"] = urlunsplit(url._replace(netloc=netloc))
                rewrote = True
    return rewrote


def _http_hook_handlers(groups: Any) -> list[dict[str, Any]]:
    """The ``"type": "http"`` handlers in one hook event's list of matcher groups."""
    if not isinstance(groups, list):
        return []
    return [
        handler
        for group in groups
        if isinstance(group, dict)
        for handler in group.get("hooks") or []
        if isinstance(handler, dict)
        and handler.get("type") == "http"
        and isinstance(handler.get("url"), str)
    ]


def _sandbox_chrome_overlay(profile_dir: Path) -> Path:
    """Throwaway dir mounted over the profile's ``chrome/`` inside the VM.

    The profile is mounted as the in-VM config dir, so claude's "Install Chrome extension"
    run *inside* the sandbox rewrites ``chrome/chrome-native-host`` to an in-VM path
    (``/home/appuser/...``). Chrome's native-messaging manifest on the host points at that
    same wrapper, so the in-VM install silently breaks the **host's** Chrome integration —
    Chrome can no longer spawn the native host. Masking the dir keeps in-VM installs inside
    the VM while leaving the host's wrapper intact.
    """
    overlay = _sandbox_state_dir(profile_dir) / "chrome"
    overlay.mkdir(exist_ok=True)
    return overlay


def _storage_cache_conf(profile_dir: Path) -> Path:
    """Write the storage.conf overlay adding the mounted store as a read-only
    additionalimagestore (regenerated each launch), and return its path."""
    conf = _sandbox_state_dir(profile_dir) / "storage.conf"
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
    dest = _sandbox_state_dir(profile_dir) / "known_hosts"
    if not dest.exists():
        dest.touch()
    return dest


def _host_file_copy(profile_dir: Path, source: Path, name: str) -> Path | None:
    """Copy a host file into the profile's state dir to mount; None when it is absent.

    Mounting the user's own file with :z would relabel it for containers, out from under
    the host's tools and restorecon (known_hosts is ssh_home_t), so the VM gets a copy,
    refreshed every launch, and :z relabels that instead.
    """
    if not source.is_file():
        return None
    copy = _sandbox_state_dir(profile_dir) / name
    copy.write_bytes(source.read_bytes())
    return copy


def _host_claude_binary() -> Path | None:
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


def _stale_claude_copy(entry: Path) -> bool:
    """Whether a file in the host-claude cache can be deleted.

    Older versions can. A ``.<version>.<pid>.partial`` copy can only once the launch
    writing it is gone: sandboxes in parallel worktrees start together, and deleting a
    copy mid-write sent that launch back to the image's own claude.
    """
    if not entry.name.endswith(".partial"):
        return True
    pid = entry.name.rsplit(".", 2)[-2]
    if not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # alive, owned by someone else
    return False


def _sandbox_claude_binary() -> Path | None:
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
            _clone_or_copy(source, partial)
            os.replace(partial, dest)
        for entry in cache.iterdir():
            if entry != dest and _stale_claude_copy(entry):
                # Another launch may be pruning the same file.
                with contextlib.suppress(FileNotFoundError):
                    entry.unlink()
    except OSError as exc:
        err_console.print(
            f"[yellow]Warning: could not cache {source} for the sandbox ({exc}); "
            f"the VM will run the image's own claude.[/yellow]"
        )
        return None
    return dest


def _clone_or_copy(source: Path, dest: Path) -> None:
    """Copy source to dest, sharing its blocks (a reflink) where the filesystem can.

    On btrfs or XFS the clone is instant and takes no space, where a copy of the claude
    binary writes 256 MB at every host update; elsewhere, or across filesystems, the
    ioctl fails and a plain copy runs. The mode is copied either way, so the binary stays
    executable.
    """
    try:
        with source.open("rb") as src, dest.open("wb") as dst:
            fcntl.ioctl(dst.fileno(), FICLONE, src.fileno())
    except OSError:
        shutil.copyfile(source, dest)
    shutil.copymode(source, dest)


def _ancestor_mcp_json(cwd: Path) -> list[Path]:
    """Return the ``.mcp.json`` files claude reads from directories above the CWD.

    claude discovers project-scoped MCP servers by walking up from the working
    directory, so a ``.mcp.json`` in an ancestor (commonly ``~/.mcp.json``) configures
    every project beneath it. Only the CWD itself is mounted into the VM, so those
    ancestors are invisible there and their servers silently vanish from the sandbox.
    Files at or below the CWD are already covered by its mount and are skipped.
    """
    return [f for parent in cwd.parents if (f := parent / ".mcp.json").is_file()]


def _mcp_json_mounts(cwd: Path) -> list[str]:
    """Read-only mounts for the ancestor ``.mcp.json`` files (see _ancestor_mcp_json).

    Mounted at their host paths so claude's upward walk finds them exactly as it does
    on the host, and read-only because a sandboxed agent has no business rewriting the
    MCP config shared by every project under that directory.
    """
    mounts: list[str] = []
    for config in _ancestor_mcp_json(cwd):
        mounts += ["-v", f"{config}:{config}:ro,z"]
    return mounts


def _ca_trust_mounts() -> list[str]:
    """Read-only mounts of the host's trust store, when it has custom CA anchors.

    Internal services signed by a private CA fail TLS in the VM otherwise: the image
    ships only public roots. The host's own update-ca-trust has already merged its
    anchors with the system roots into the extracted bundles that curl, git and openssl
    read, so the VM gets those as they are; running update-ca-trust in the VM took about
    7 s of every launch. The anchors come too, at the path the entrypoint checks before
    pointing node and python at the bundle. Same paths as on the host, so the bundles'
    links still resolve.

    No ``:z`` here, unlike every other mount: relabelling is for paths the container
    must *write*, and these are read-only system state. SELinux already lets containers
    read ``cert_t``, while ``:z`` would relabel root-owned system directories out from
    under the host's own TLS clients (and fail for a rootless podman that cannot chcon
    them in the first place).
    """
    anchors = Path(SANDBOX_CA_ANCHORS)
    extracted = Path(SANDBOX_CA_EXTRACTED)
    try:
        if not any(anchors.iterdir()):
            return []
    except OSError:
        return []
    if not extracted.is_dir():
        err_console.print(
            f"[yellow]Warning: {anchors} has CA anchors but there is no {extracted}, "
            f"so the sandbox does not trust them. Run update-ca-trust on the host.[/yellow]"
        )
        return []
    return ["-v", f"{anchors}:{anchors}:ro", "-v", f"{extracted}:{extracted}:ro"]


def _sandbox_mounts(profile_dir: Path, cwd: Path) -> list[str]:
    """Build podman -v args: profile config, cwd, and the git common dir.

    Mounts use ``:z`` (SELinux relabel) only. ``:U`` is deliberately omitted:
    ``--userns=keep-id`` already maps the host UID into the VM, while ``:U`` would
    recursively chown the mounted tree to the container's run-user (root, which maps
    to a subuid), wrecking ownership of the user's project on the host.
    """
    root = _sandbox_work_root(cwd)
    mounts = [
        "-v",
        f"{profile_dir}:{SANDBOX_CONFIG_DIR}:z",
        "-v",
        f"{root}:{root}:z",
    ]
    # Mask the profile's chrome/ dir: an in-VM native-host install must not rewrite the
    # host's wrapper, which Chrome's manifest points at (see _sandbox_chrome_overlay).
    mounts += [
        "-v",
        f"{_sandbox_chrome_overlay(profile_dir)}:{SANDBOX_CONFIG_DIR}/chrome:z",
    ]
    # Override just settings.json inside the VM; writes land in the throwaway overlay
    # (regenerated each launch), not the profile's real settings.json.
    overlay = _sandbox_settings_overlay(profile_dir)
    mounts += ["-v", f"{overlay}:{SANDBOX_CONFIG_DIR}/settings.json:z"]
    git_dir = _git_common_dir(cwd)
    if git_dir is not None and git_dir != root and root not in git_dir.parents:
        mounts += ["-v", f"{git_dir}:{git_dir}:z"]
    mounts += _git_state_mounts(profile_dir, cwd)
    mounts += _local_settings_mounts(profile_dir, root, cwd)
    gitconfig = _host_file_copy(profile_dir, Path.home() / ".gitconfig", "gitconfig")
    if gitconfig is not None:
        mounts += ["-v", f"{gitconfig}:{SANDBOX_GITCONFIG}:ro,z"]
    host_known_hosts = _host_file_copy(
        profile_dir, Path.home() / ".ssh" / "known_hosts", "host_known_hosts"
    )
    if host_known_hosts is not None:
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
    mounts += _mcp_json_mounts(root)
    mounts += _ca_trust_mounts()
    mounts += _linked_mounts(profile_dir)
    mounts += _launch_state_mounts(profile_dir)
    mounts += _image_cache_mounts(profile_dir)
    return mounts


def _launch_state_mounts(profile_dir: Path) -> list[str]:
    """Read-only mounts pinning the profile files that decide how it launches.

    The VM sees the profile dir read-write. Deleting .sandbox would turn the next plain
    launch into a host launch, and .env is loaded into that launch's environment
    (LD_PRELOAD, say). Mounted read-only over themselves, neither can be changed or
    removed from inside. A missing .env is created empty so there is a file to pin; the
    VM gets the values through the env file (see _secret_env_file).
    """
    env_file = profile_dir / ".env"
    if not os.path.lexists(env_file):
        os.close(os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    mounts: list[str] = []
    for name in (SANDBOX_MARKER, ".env"):
        path = profile_dir / name
        if path.is_file() and not path.is_symlink():
            mounts += ["-v", f"{path}:{SANDBOX_CONFIG_DIR}/{name}:ro,z"]
    return mounts


def _git_state_mounts(profile_dir: Path, cwd: Path) -> list[str]:
    """Keep the VM from changing the git config and hooks that the host runs.

    The work tree and its git dir are mounted read-write, so the agent could otherwise
    set core.fsmonitor, core.sshCommand or an alias in .git/config, or plant a hook, and
    the host would run it at its next git command in the repo: a shell prompt's
    `git status` is enough, even while the VM is still running. Each git dir's config,
    the repo's and its submodules', is mounted read-only over itself. git writes config
    by renaming a lock file over it, which the mount refuses, so commands that save
    settings there (push -u, branch -u, remote add) cannot save them; the briefing says
    so. hooks/ becomes a throwaway copy (see _sandbox_hooks_copy).
    """
    common = _git_common_dir(cwd)
    if common is None:
        return []
    mounts: list[str] = []
    for git_dir in [common, *_submodule_git_dirs(common)]:
        mounts += _git_dir_mounts(profile_dir, git_dir)
    return mounts


def _git_dir_mounts(profile_dir: Path, git_dir: Path) -> list[str]:
    """Pin one git dir's config read-only and give the VM a copy of its hooks."""
    config = git_dir / "config"
    hooks = git_dir / "hooks"
    for path in (config, hooks):
        if path.is_symlink():
            err_console.print(
                f"[yellow]Warning: {path} is a symlink, which git does not create, so the "
                f"sandbox cannot protect it. Check where it points.[/yellow]"
            )
    mounts: list[str] = []
    if config.is_file() and not config.is_symlink():
        mounts += ["-v", f"{config}:{config}:ro,z"]
    if not hooks.is_symlink():
        mounts += ["-v", f"{_sandbox_hooks_copy(profile_dir, hooks)}:{hooks}:z"]
    return mounts


def _submodule_git_dirs(git_dir: Path) -> list[Path]:
    """Git dirs of the repo's submodules under modules/, nested submodules included."""
    found: list[Path] = []
    pending = [git_dir / "modules"]
    while pending:
        current = pending.pop()
        if not current.is_dir() or current.is_symlink():
            continue
        for child in sorted(current.iterdir()):
            # A link here could pass off another repo's git dir as a submodule's.
            if child.is_symlink() or not child.is_dir():
                continue
            if (child / "HEAD").is_file() and (child / "objects").is_dir():
                found.append(child)
                pending.append(child / "modules")
            else:
                # A submodule named vendor/lib keeps its git dir in modules/vendor/lib.
                pending.append(child)
    return found


def _sandbox_hooks_copy(profile_dir: Path, hooks: Path) -> Path:
    """Refresh the VM's throwaway copy of a git dir's hooks/ and return its path.

    The host's hooks keep running in the VM and `prek install` works there, but nothing
    the VM writes reaches the hooks the host runs: each launch starts again from the
    host's. Links are copied as links, never followed, which would copy whatever they
    point at into the VM.
    """
    key = hashlib.sha256(str(hooks).encode()).hexdigest()[:16]
    copy = _sandbox_state_dir(profile_dir) / "git-hooks" / key
    copy.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        for entry in copy.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    except OSError as exc:
        err_console.print(
            f"[red]Could not clear the sandbox's copy of {hooks} at {copy} ({exc}).[/red] "
            f"A container in the VM may have written files your user cannot delete; "
            f"remove them with: podman unshare rm -r {copy}"
        )
        sys.exit(1)
    if hooks.is_dir():
        try:
            shutil.copytree(hooks, copy, symlinks=True, dirs_exist_ok=True)
        except OSError as exc:
            err_console.print(
                f"[yellow]Warning: could not copy every hook from {hooks} into the "
                f"sandbox ({exc}); git in the VM runs without the missing ones.[/yellow]"
            )
    return copy


def _local_settings_paths(root: Path, cwd: Path) -> list[Path]:
    """The .claude/settings.local.json files claude reads for a launch in cwd."""
    return [
        base / ".claude" / "settings.local.json" for base in dict.fromkeys((root, cwd))
    ]


def _local_settings_mounts(profile_dir: Path, root: Path, cwd: Path) -> list[str]:
    """Per-launch copies of the project's .claude/settings.local.json for the VM.

    claude on the host loads hooks from this file at the work tree root and at the
    launch dir, and git ignores it, so a change made in the VM would run on the host
    unnoticed. Where the file exists the VM gets a copy instead, never read through a
    link (see _sandbox_settings_overlay). Where it does not, mounting one would create it
    on the host, so a supervised session reports one the VM creates instead.
    """
    mounts: list[str] = []
    for local in _local_settings_paths(root, cwd):
        if not os.path.lexists(local) or local.is_dir():
            continue
        content = b"{}\n"
        if local.is_symlink():
            err_console.print(
                f"[yellow]Warning: {local} is a symlink, so the sandbox gets empty "
                f"local settings there instead of reading through it.[/yellow]"
            )
        else:
            content = local.read_bytes()
        key = hashlib.sha256(str(local).encode()).hexdigest()[:16]
        copy = _sandbox_state_dir(profile_dir) / "local-settings" / f"{key}.json"
        copy.parent.mkdir(mode=0o700, exist_ok=True)
        copy.write_bytes(content)
        mounts += ["-v", f"{copy}:{local}:z"]
    return mounts


def _linked_mounts(profile_dir: Path) -> list[str]:
    """Read-only mounts for the profile entries symlinked into the global ~/.claude.

    The profile dir is mounted as the in-VM config dir, but a symlinked commands/,
    skills/ or statusline.sh points at an absolute host path (e.g. ~/.claude/skills)
    that is not otherwise mounted, so the link dangles inside the VM. Mount the real
    target at the link's path, read-only so a sandboxed agent cannot modify what every
    profile shares.

    Only links pointing at the global ``~/.claude/<name>`` that ``add`` and ``links``
    create are mounted. The profile dir is writable from inside the VM, so a link aimed
    anywhere else may have been planted there to get that host path mounted next launch.
    """
    mounts: list[str] = []
    for name in (*LINKABLE_DIRS, STATUSLINE_FILE):
        link = profile_dir / name
        if not link.is_symlink():
            continue
        target = Path(os.readlink(link))
        expected = Path.home() / ".claude" / name
        if target != expected:
            err_console.print(
                f"[yellow]Warning: not mounting {link} into the sandbox: it points at "
                f"{_shown(str(target))}, not {expected}. Re-point it with: "
                f"ln -sfn {expected} {link}[/yellow]"
            )
            continue
        real = link.resolve()
        if real.exists():
            mounts += ["-v", f"{real}:{target}:ro,z"]
    return mounts


@dataclass
class _Forwarding:
    """What a launch bridges into the VM; a bridges.BridgeServer serves it."""

    forwards: list[tuple[Path, Path, str]]  # (host_socket, guest_path, service name)
    _: KW_ONLY
    ssh_auth_sock: Path | None = None
    gpg_pubkeys: bytes | None = None  # host public keyring (gpg --export)
    clipboard: bool = False  # read-only host clipboard (the in-VM wl-paste shim)

    def gpg(self) -> bool:
        """True when the host gpg-agent is bridged into the VM."""
        return any(service == "gpg" for _host, _guest, service in self.forwards)

    def active(self) -> bool:
        """True when the launch needs the bridge server at all."""
        return bool(self.forwards) or self.clipboard


def _build_sandbox_argv(
    profile_dir: Path,
    cwd: Path,
    claude_args: list[str],
    extra_env: dict[str, str],
    forwarding: _Forwarding | None = None,
    *,
    host_loopback: bool = False,
    bridge: tuple[int, str] | None = None,
    vault_ca: Path | None = None,
) -> list[str]:
    """Assemble the `podman run` argv that boots claude in a krun microVM."""
    # A TTY only when both ends are terminals: with one, the in-VM claude takes its stdin
    # for a TTY and drops piped input (`git diff | claude-profile work -p "review"`).
    tty = ["-t"] if sys.stdin.isatty() and sys.stdout.isatty() else []
    argv = [
        settings.podman_bin,
        "run",
        "--rm",
        "-i",
        *tty,
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
        # podman otherwise passes the host's *_proxy variables in, a proxy URL's
        # credentials included; the VM gets only what the launcher forwards.
        "--http-proxy=false",
        "--device",
        "/dev/kvm",
    ]
    if host_loopback or (forwarding and forwarding.active()):
        # pasta gives the VM a route to the host (TSI cannot); --map-host-loopback
        # makes host.containers.internal reach the host's loopback, so the agent
        # bridge can bind to 127.0.0.1 rather than every host interface. Shared HTTP
        # hooks aimed at the host's loopback (host_loopback) need the same route.
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
    argv += _git_identity_mounts(
        cwd, signing=forwarding is not None and forwarding.gpg()
    )
    argv += _forwarding_env(forwarding)
    if bridge is not None:
        port, token = bridge
        argv += ["-e", f"{SANDBOX_BRIDGE_PORT_ENV}={port}"]
        extra_env = {**extra_env, SANDBOX_BRIDGE_TOKEN_ENV: token}
    if extra_env:
        argv += ["--env-file", _secret_env_file(extra_env)]
    mounts = _sandbox_mounts(profile_dir, cwd)
    if any(spec.split(":")[1:2] == [SANDBOX_HOST_CLAUDE] for spec in mounts):
        # The VM runs the host's binary from a read-only mount and is thrown away at
        # exit, so an in-VM self-update would download a release only to discard it —
        # and would move the session off the host's version mid-run. Without the mount
        # (no native install, or the copy failed) the image's claude may update itself.
        argv += ["-e", "DISABLE_AUTOUPDATER=1"]
    argv += mounts
    if vault_ca is not None:
        # Over the VM's system bundle, after any host trust store mount, so every default
        # path (/etc/ssl/cert.pem and the rest link to it) trusts the proxy's CA too.
        argv += ["-v", f"{vault_ca}:{SANDBOX_VM_CA_BUNDLE}:ro,z"]
    argv += _gpg_pubkeys_mounts(forwarding)
    argv += ["-w", str(cwd), settings.sandbox_image, "claude"]
    return argv + _sandbox_claude_args(claude_args, extra_env)


def _sandbox_claude_args(
    claude_args: list[str], extra_env: dict[str, str]
) -> list[str]:
    """claude's arguments in the VM: the sandbox's own flags, then the user's.

    The sandbox's flags go first: after a subcommand's `--` they would become that
    command's arguments (`claude mcp add NAME -- CMD ...` saved them into the server
    definition).
    """
    args = list(claude_args)
    session: list[str] = []
    # An explicit --permission-mode wins: claude ranks the skip flag above it, so adding
    # the flag would silently turn e.g. a plan-mode run into bypassPermissions.
    if (
        settings.sandbox_skip_permissions
        and SKIP_PERMISSIONS_FLAG not in args
        and not _has_option(args, "--permission-mode")
    ):
        session.append(SKIP_PERMISSIONS_FLAG)
    # claude force-disables Chrome in a non-interactive session (dn()=!isInteractive),
    # which the sandbox launch trips, so claudeInChromeDefaultEnabled never applies. The
    # explicit --chrome flag is checked first, so add it to actually enable the
    # integration when the user opted into sandbox_chrome.
    if settings.sandbox_chrome and "--chrome" not in args and "--no-chrome" not in args:
        session.append("--chrome")
    if not _has_option(args, "--append-system-prompt"):
        session += [
            "--append-system-prompt",
            SANDBOX_BRIEFING
            + _infisical_briefing(extra_env)
            + _agent_vault_briefing(extra_env),
        ]
    return session + args


def _git_config_get(cwd: Path, key: str) -> str | None:
    """The value git resolves for key in cwd on the host, or None if unset."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def _git_identity_mounts(cwd: Path, *, signing: bool) -> list[str]:
    """Mount and env args giving the VM the git identity git uses for cwd on the host.

    Only ~/.gitconfig is mounted into the VM. The files it pulls in with include or
    includeIf are not, and an ``includeIf "gitdir:~/..."`` could not match there anyway
    (``~`` is /home/appuser in the VM), so sandbox commits fell back to the default
    identity. GIT_CONFIG_GLOBAL points the VM at a file that includes the mounted
    ~/.gitconfig and then sets the values resolved here, so they act as global config
    and a repo's own config still overrides them. Signing settings come along only when
    GPG is forwarded and the format is openpgp: an SSH or X.509 setup can't sign in the
    VM, and passing it would only make commits fail.
    """
    keys = GIT_IDENTITY_KEYS + (GIT_SIGNING_KEYS if signing else ())
    values = {
        key: value for key in keys if (value := _git_config_get(cwd, key)) is not None
    }
    if values.get("gpg.format", "openpgp") != "openpgp":
        values = {key: v for key, v in values.items() if key not in GIT_SIGNING_KEYS}
    if not values:
        return []
    lines = ["[include]", f"\tpath = {SANDBOX_GITCONFIG}"]
    for key, value in values.items():
        section, name = key.split(".", 1)
        quoted = value.replace("\\", "\\\\").replace('"', '\\"')
        lines += [f"[{section}]", f'\t{name} = "{quoted}"']
    content = "\n".join(lines) + "\n"
    digest = hashlib.sha256(content.encode()).hexdigest()[:16]
    identity = _data_dir() / "git" / f"identity-{digest}"
    if not identity.exists():
        identity.parent.mkdir(parents=True, exist_ok=True)
        partial = identity.with_name(f".{identity.name}.{os.getpid()}.partial")
        partial.write_text(content)
        os.replace(partial, identity)
    return [
        "-v",
        f"{identity}:{SANDBOX_GIT_IDENTITY}:ro,z",
        "-e",
        f"GIT_CONFIG_GLOBAL={SANDBOX_GIT_IDENTITY}",
    ]


def _secret_env_file(env: dict[str, str]) -> str:
    """Write env to an unlinked podman ``--env-file`` and return its ``/dev/fd`` path.

    These values include tokens and the profile's ``.env``. Given as ``-e KEY=VALUE``
    they would sit in podman's argv for the whole session, readable by every local user
    in /proc/<pid>/cmdline. Putting them in podman's own environment instead would let
    a ``.env`` written from inside the VM set LD_PRELOAD and the like for the host
    podman. The file lives in the per-user tmpfs runtime dir and is unlinked at once,
    so it never reaches disk and has no name: podman reads it through the inherited fd.
    (os.memfd_create would do the same, but uv's standalone Pythons lack it.)
    """
    multiline = sorted(key for key, value in env.items() if "\n" in value)
    if multiline:
        err_console.print(
            f"[red]Can't pass {', '.join(multiline)} into the sandbox: the value "
            f"contains a newline, and podman reads one variable per line.[/red]"
        )
        sys.exit(1)
    fd, path = tempfile.mkstemp(dir=os.environ.get("XDG_RUNTIME_DIR"))
    os.unlink(path)
    with open(fd, "w", closefd=False) as env_file:
        env_file.writelines(f"{key}={value}\n" for key, value in env.items())
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)
    return f"/dev/fd/{fd}"


def _forwarding_env(forwarding: _Forwarding | None) -> list[str]:
    """Env args telling the entrypoint which sockets to bridge and how."""
    if forwarding is None or not forwarding.active():
        return []
    env: list[str] = []
    sockets = [f"{guest}={service}" for _host, guest, service in forwarding.forwards]
    if sockets:
        env += ["-e", f"CLAUDE_SANDBOX_FORWARDS={','.join(sockets)}"]
    if forwarding.ssh_auth_sock is not None:
        env += ["-e", f"SSH_AUTH_SOCK={forwarding.ssh_auth_sock}"]
    if forwarding.gpg_pubkeys is not None:
        env += [
            "-e",
            f"GNUPGHOME={SANDBOX_GNUPGHOME}",
            "-e",
            f"CLAUDE_SANDBOX_GPG_PUBKEYS_FILE={SANDBOX_GPG_PUBKEYS}",
        ]
    return env


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
            check=False,
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


def _gpg_extra_socket() -> Path | None:
    """Path to the host gpg-agent restricted (signing-only) socket, starting one if needed.

    gpgconf reports the path whether or not an agent is running, but the socket only
    exists while one is, and nothing guarantees the host has used gpg yet this login.
    Without the launch a sandbox started at the wrong moment forwards no GPG at all for
    its whole life, so ask gpgconf to bring the agent up before giving up.
    """
    try:
        result = subprocess.run(
            ["gpgconf", "--list-dirs", "agent-extra-socket"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    path = result.stdout.strip()
    if result.returncode != 0 or not path:
        return None
    sock = Path(path)
    if not sock.exists():
        subprocess.run(
            ["gpgconf", "--launch", "gpg-agent"], capture_output=True, check=False
        )
    return sock if sock.exists() else None


def _export_gpg_pubkeys() -> bytes | None:
    """Export of the host public keyring (no secret material), or None."""
    try:
        result = subprocess.run(["gpg", "--export"], capture_output=True, check=False)
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def _gpg_pubkeys_mounts(forwarding: _Forwarding | None) -> list[str]:
    """Read-only mount of the host public keyring for the entrypoint to import.

    A file rather than an env var: Linux caps a single argv or env string at 128 KiB,
    and a keyring with a few dozen keys passes that, failing the launch with E2BIG.
    Written to our data dir (not the VM-writable profile dir) under a temp name and
    renamed, so parallel launches never mount a half-written file.
    """
    if forwarding is None or forwarding.gpg_pubkeys is None:
        return []
    keyring = _data_dir() / "gpg-pubkeys"
    partial = keyring.with_name(f".gpg-pubkeys.{os.getpid()}.partial")
    try:
        keyring.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(forwarding.gpg_pubkeys)
        os.replace(partial, keyring)
    except OSError as exc:
        err_console.print(
            f"[red]Can't write your GPG public keys for the sandbox to {keyring}: "
            f"{exc}[/red]"
        )
        sys.exit(1)
    return ["-v", f"{keyring}:{SANDBOX_GPG_PUBKEYS}:ro,z"]


def _build_forwarding() -> _Forwarding:
    """Collect the agent forwards requested via settings."""
    forwards: list[tuple[Path, Path, str]] = []
    ssh_auth: Path | None = None
    if settings.sandbox_ssh_agent:
        ssh = [
            (sock, Path(SANDBOX_AGENT_DIR) / f"ssh-agent-{index}.sock", f"ssh-{index}")
            for index, sock in enumerate(_ssh_agent_sockets())
        ]
        forwards += ssh
        if ssh:
            ssh_auth = ssh[0][1]
        else:
            err_console.print(
                "[yellow]Warning: CLAUDE_PROFILE_SANDBOX_SSH_AGENT is set but no live SSH "
                "agent was found (SSH_AUTH_SOCK or ~/.1password/agent.sock), so ssh in the "
                "sandbox has no keys. Check 'ssh-add -l'.[/yellow]"
            )
    pubkeys: bytes | None = None
    if settings.sandbox_gpg_agent:
        extra = _gpg_extra_socket()
        if extra is not None:
            guest = Path(SANDBOX_GNUPGHOME) / "S.gpg-agent"
            forwards.append((extra, guest, "gpg"))
            pubkeys = _export_gpg_pubkeys()
        else:
            err_console.print(
                "[yellow]Warning: CLAUDE_PROFILE_SANDBOX_GPG_AGENT is set but no host "
                "gpg-agent socket is available, so GPG is not forwarded and signing will "
                "fail in the sandbox. Check 'gpgconf --launch gpg-agent'.[/yellow]"
            )
    return _Forwarding(
        forwards,
        ssh_auth_sock=ssh_auth,
        gpg_pubkeys=pubkeys,
        clipboard=settings.sandbox_clipboard,
    )


def _bridge_services(forwarding: _Forwarding) -> dict[str, bridges.Service]:
    """The bridge server's services for a launch, by the names the VM asks for."""
    services = {
        service: bridges.unix_relay(host)
        for host, _guest, service in forwarding.forwards
    }
    if forwarding.clipboard:
        services["clipboard"] = bridges.clipboard
    return services


def _profile_oauth_scopes(profile_dir: Path) -> list[str] | None:
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
    a re-login. Unreadable credentials are left alone — claude reports auth problems itself.
    """
    scopes = _profile_oauth_scopes(profile_dir)
    if scopes is None or CHROME_OAUTH_SCOPES & set(scopes):
        return
    err_console.print(
        f"[yellow]Warning: CLAUDE_PROFILE_SANDBOX_CHROME is set but profile "
        f"'{profile_dir.name}' has OAuth scopes {sorted(scopes)}, none of which claude "
        f"accepts for Claude in Chrome (needs one of {sorted(CHROME_OAUTH_SCOPES)}). "
        f"Chrome will report 'Disabled'. A setup-token login "
        f"grants user:inference only — re-authenticate with a full OAuth login: "
        f"CLAUDE_PROFILE_SANDBOX=0 claude-profile {profile_dir.name} /login[/yellow]"
    )


def _gh_token() -> str | None:
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
            check=False,
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


def _infisical_token(email: str) -> str | None:
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
            check=False,
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
    """True if an allowlist entry names this login.

    An entry with an @ must equal the login's email. Any other entry is a domain that
    must equal, or be a parent of, the email's domain or the login's Infisical host.
    Matching substrings forwarded tokens for logins the user never named (bob@ also
    picked up jimbob@).
    """
    email = (user.get("email") or "").lower()
    if "@" in entry:
        return entry == email
    host = (urlsplit(user.get("domain") or "").hostname or "").lower()
    names = [name for name in (email.rpartition("@")[2], host) if name]
    return any(name == entry or name.endswith(f".{entry}") for name in names)


def _infisical_logins() -> list[_InfisicalLogin]:
    """Resolve the allowlisted, still-valid infisical logins to forward.

    ``sandbox_infisical`` is a comma-separated allowlist of emails or domains (see
    _infisical_login_matches). Each entry is matched against the host's logged-in users;
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


def _pulumi_token() -> str | None:
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


@dataclass(frozen=True)
class _AgentVault:
    """A profile's Agent Vault settings: the CLAUDE_PROFILE_AGENT_VAULT_* keys in its .env."""

    bundle: str
    proxy: str
    ca_fingerprint: str
    infisical_profile: str = ""
    no_proxy: tuple[str, ...] = ()


@dataclass
class _VaultRun:
    """How an opted-in launch goes: the CLI's variables when brokered, None when not."""

    session: dict[str, str] | None

    def state(self) -> str:
        """The AGENT_VAULT_STATE_ENV value the VM gets."""
        return "unavailable" if self.session is None else "active"


def _split_agent_vault(
    profile_dir: Path, profile_env: dict[str, str]
) -> tuple[_AgentVault | None, dict[str, str]]:
    """Separate the profile's Agent Vault keys from the env claude gets.

    BUNDLE switches the vault on; unset, the profile launches without it and keeps the
    other keys for next time. A misspelled key, or BUNDLE without the keys it needs,
    fails the launch rather than quietly running without the vault.
    """
    rest = {
        k: v for k, v in profile_env.items() if not k.startswith(AGENT_VAULT_PREFIX)
    }
    keys = {
        k.removeprefix(AGENT_VAULT_PREFIX): v
        for k, v in profile_env.items()
        if k.startswith(AGENT_VAULT_PREFIX)
    }
    problems = _agent_vault_problems(keys)
    if problems:
        err_console.print(
            f"[red]The Agent Vault settings in {profile_dir / '.env'} can't be used: "
            f"{escape('; '.join(problems))}. Fix them with `claude-profile env "
            f"{profile_dir.name} --set KEY=VALUE`, or unset {AGENT_VAULT_PREFIX}BUNDLE "
            f"to launch without Agent Vault.[/red]"
        )
        sys.exit(1)
    if not keys.get("BUNDLE"):
        return None, rest
    hosts = keys.get("NO_PROXY", "").split(",")
    vault = _AgentVault(
        bundle=keys["BUNDLE"],
        proxy=keys["PROXY"],
        ca_fingerprint=keys["CA_FINGERPRINT"].upper(),
        infisical_profile=keys.get("INFISICAL_PROFILE", ""),
        no_proxy=tuple(host.strip() for host in hosts if host.strip()),
    )
    return vault, rest


def _agent_vault_problems(keys: dict[str, str]) -> list[str]:
    """What is wrong with a profile's Agent Vault keys (prefix removed), if anything."""
    known = AGENT_VAULT_REQUIRED + AGENT_VAULT_OPTIONAL
    problems = [
        f"{AGENT_VAULT_PREFIX}{key} is not a setting"
        for key in sorted(keys)
        if key not in known
    ]
    if keys.get("BUNDLE"):
        problems += [
            f"{AGENT_VAULT_PREFIX}{key} is not set"
            for key in AGENT_VAULT_REQUIRED
            if not keys.get(key)
        ]
    fingerprint = keys.get("CA_FINGERPRINT", "")
    if fingerprint and not AGENT_VAULT_FINGERPRINT.fullmatch(fingerprint.upper()):
        problems.append(
            f"{AGENT_VAULT_PREFIX}CA_FINGERPRINT is not a fingerprint like SHA256:9F:2C:... "
            f"(32 bytes), as the Proxies page shows it"
        )
    return problems


def _agent_vault_ca_fingerprint(proxy: str) -> str:
    """The SHA-256 fingerprint of the CA the proxy serves, written as the Proxies page has it.

    Asked directly, never through a proxy: the launcher's environment may name one.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://{proxy}/_agent-vault/ca"
    with opener.open(url, timeout=AGENT_VAULT_PREFLIGHT_TIMEOUT) as response:
        body = json.load(response)
    certificate = body.get("certificate") if isinstance(body, dict) else None
    if not isinstance(certificate, str):
        raise TypeError("the response has no certificate")
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest().upper()
    return "SHA256:" + ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _agent_vault_preflight(vault: _AgentVault) -> str | None:
    """Why this launch can't use the vault, or None when the proxy serves the pinned CA.

    `infisical agent-vault run` checks the same pin, but its failure ends the launch.
    Checking first lets the usual outage, the proxy being down, launch without the vault.
    """
    if shutil.which(settings.infisical_bin) is None:
        return f"the infisical CLI ({settings.infisical_bin}) is not installed"
    try:
        served = _agent_vault_ca_fingerprint(vault.proxy)
    except (OSError, ValueError, TypeError) as exc:
        return f"the proxy at {vault.proxy} did not serve its CA ({exc})"
    if served != vault.ca_fingerprint:
        return (
            f"the proxy at {vault.proxy} serves the CA {served}, not the pinned "
            f"{vault.ca_fingerprint}"
        )
    return None


def _exec_agent_vault_run(
    name: str, vault: _AgentVault, claude_args: list[str]
) -> None:
    """Replace this process with `infisical agent-vault run` around this same launch.

    The CLI mints a session over the profile's access bundle with your Infisical login,
    checks the proxy's CA against the pin and starts the launch again as its child, with
    the proxy and CA variables set. When the child exits, on SIGHUP or SIGTERM too, it
    revokes the session. The child finds its profile's name in AGENT_VAULT_RUN_ENV and
    goes on to claude. Unlike every other launch, this one leaves a process between the
    terminal and claude: the CLI has to outlive claude to revoke the session.
    """
    argv = [settings.infisical_bin, "agent-vault", "run", "--silent"]
    if vault.infisical_profile:
        argv += ["--profile", vault.infisical_profile]
    argv += [
        "--access-bundle",
        vault.bundle,
        "--proxy",
        vault.proxy,
        "--ca-fingerprint",
        vault.ca_fingerprint,
        "--ttl",
        AGENT_VAULT_SESSION_TTL,
        "--no-proxy",
        ",".join((*AGENT_VAULT_NO_PROXY, *vault.no_proxy)),
        "--",
        sys.argv[0],
        name,
        *claude_args,
    ]
    os.execvpe(settings.infisical_bin, argv, {**os.environ, AGENT_VAULT_RUN_ENV: name})


def _route_agent_vault(
    name: str, vault: _AgentVault, claude_args: list[str]
) -> _VaultRun | None:
    """Send a launch that opted in through the vault; None once it has exec'd the CLI.

    The launch the CLI starts takes the CLI's variables. Before that, a pre-flight
    decides: with the proxy serving the pinned CA the launch execs the CLI, otherwise it
    goes ahead without the vault and says so.
    """
    if os.environ.get(AGENT_VAULT_RUN_ENV) == name:
        return _VaultRun(_take_agent_vault_session())
    reason = _agent_vault_preflight(vault)
    if reason is None:
        _exec_agent_vault_run(name, vault, claude_args)
        return None
    err_console.print(
        f"[bold red]Agent Vault is unavailable: {escape(reason)}. Launching {name} without "
        f"it, so the services it brokers won't authenticate this session.[/bold red]"
    )
    return _VaultRun(None)


def _take_agent_vault_session() -> dict[str, str]:
    """Remove the variables `infisical agent-vault run` set from this process; return them.

    What the launch runs on the host from here on (podman, git, gh) gets the host's own
    environment back, not a proxy and a CA file holding only the proxy's root. Each
    launch path then gives claude its share.
    """
    os.environ.pop(AGENT_VAULT_RUN_ENV, None)
    names = (*AGENT_VAULT_SESSION_VARS, *AGENT_VAULT_PROXY_VARS, *AGENT_VAULT_CA_VARS)
    session = {name: os.environ.pop(name) for name in names if name in os.environ}
    if "HTTPS_PROXY" not in session or "SSL_CERT_FILE" not in session:
        err_console.print(
            f"[red]{AGENT_VAULT_RUN_ENV} is set, but HTTPS_PROXY or SSL_CERT_FILE is not, so "
            f"this launch did not come from `infisical agent-vault run`. Unset "
            f"{AGENT_VAULT_RUN_ENV} and launch again.[/red]"
        )
        sys.exit(1)
    return session


def _system_ca_bundle() -> Path:
    """The host's system CA bundle, where OpenSSL was built to look for it."""
    return Path(ssl.get_default_verify_paths().openssl_cafile)


def _agent_vault_ca_bundle(proxy_ca: Path) -> Path:
    """Write the host's system CA bundle with the proxy's CA appended; return the file.

    The CLI points every TLS variable at a file holding only the proxy's CA, so HTTPS
    that skips the proxy (the NO_PROXY hosts, claude's own among them) fails to verify
    in curl, git and Python. With the system roots alongside, proxied and direct
    connections both verify. Named by content, like the git identity files.
    """
    try:
        content = f"{_system_ca_bundle().read_text().rstrip()}\n{proxy_ca.read_text()}"
    except OSError as exc:
        err_console.print(
            f"[red]Can't build the CA bundle for Agent Vault: {escape(str(exc))}.[/red]"
        )
        sys.exit(1)
    digest = hashlib.sha256(content.encode()).hexdigest()[:16]
    bundle = _data_dir() / "agent-vault" / f"ca-bundle-{digest}.pem"
    if not bundle.exists():
        bundle.parent.mkdir(parents=True, exist_ok=True)
        partial = bundle.with_name(f".{bundle.name}.{os.getpid()}.partial")
        partial.write_text(content)
        os.replace(partial, bundle)
    return bundle


def _agent_vault_host_env(session: dict[str, str]) -> dict[str, str]:
    """A host launch's share of the session: all of it, with TLS on the combined bundle."""
    bundle = str(_agent_vault_ca_bundle(Path(session["SSL_CERT_FILE"])))
    return {**session, **dict.fromkeys(AGENT_VAULT_CA_VARS, bundle)}


def _with_agent_vault(
    extra_env: dict[str, str], vault_run: _VaultRun | None
) -> tuple[dict[str, str], Path | None]:
    """Add the vault's share of the VM env; return it with the CA bundle to mount, if any.

    The proxy URLs carry the session token, so they travel in the secret env file with
    the rest of extra_env; the CLI's copy for OpenClaw stays out. The VM's NO_PROXY adds
    the host's loopback, where bridged services and loopback hooks listen.
    """
    if vault_run is None:
        return extra_env, None
    env = {**extra_env, AGENT_VAULT_STATE_ENV: vault_run.state()}
    session = vault_run.session
    if session is None:
        return env, None
    hosts = (
        session.get("NO_PROXY", ""),
        SANDBOX_HOST_LOOPBACK,
        "host.containers.internal",
    )
    no_proxy = ",".join(host for host in hosts if host)
    env.update(
        {name: session[name] for name in AGENT_VAULT_PROXY_URL_VARS if name in session}
    )
    env.update({"NO_PROXY": no_proxy, "no_proxy": no_proxy, "NODE_USE_ENV_PROXY": "1"})
    env.update(dict.fromkeys(AGENT_VAULT_CA_VARS, SANDBOX_VM_CA_BUNDLE))
    return env, _agent_vault_ca_bundle(Path(session["SSL_CERT_FILE"]))


def _agent_vault_briefing(extra_env: dict[str, str]) -> str:
    """The system-prompt note on brokered credentials, for a launch that opted in."""
    state = extra_env.get(AGENT_VAULT_STATE_ENV)
    if state == "active":
        return AGENT_VAULT_BRIEFING
    if state == "unavailable":
        return AGENT_VAULT_UNAVAILABLE_BRIEFING
    return ""


def _launch_sandbox(
    profile_dir: Path,
    claude_args: list[str],
    extra_env: dict[str, str],
    *,
    vault_run: _VaultRun | None = None,
) -> None:
    """Launch a podman krun microVM running claude (optionally bridging agents)."""
    _refuse_home_mount(Path.cwd())
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
    extra_env = _with_gh_token(extra_env)
    extra_env = _with_infisical_env(extra_env)
    extra_env = _with_pulumi_token(extra_env)
    extra_env = _with_forwarded_env(extra_env)
    extra_env, vault_ca = _with_agent_vault(extra_env, vault_run)
    shared, host_loopback = _shared_settings_args(
        profile_dir.name, claude_args, sandbox=True
    )
    claude_args = [*shared, *claude_args]
    cwd = Path.cwd()
    forwarding = _build_forwarding()
    if forwarding.active():
        _run_sandbox_supervised(
            profile_dir, cwd, claude_args, extra_env, forwarding, vault_ca=vault_ca
        )
        return
    argv = _build_sandbox_argv(
        profile_dir,
        cwd,
        claude_args,
        extra_env,
        host_loopback=host_loopback,
        vault_ca=vault_ca,
    )
    os.execvpe(settings.podman_bin, argv, os.environ.copy())


def _refuse_home_mount(cwd: Path) -> None:
    """Exit when the directory mounted for cwd is the home directory or above it.

    The sandbox mounts cwd's work tree (see _sandbox_work_root) read-write, so ~ or /
    would hand the VM the whole home directory: ~/.ssh private keys, keyrings and every
    profile's credentials. A dotfiles repo at ~ makes ~ the work tree of any directory
    under it. A root inside the profiles dir, or around it, is refused for the same
    reason.
    """
    root = _sandbox_work_root(cwd).resolve()
    home = Path.home().resolve()
    if root == home or root in home.parents:
        err_console.print(
            f"[red]Refusing to start the sandbox in {cwd}: it would mount {root} "
            f"read-write, which includes your whole home directory. cd into a project "
            f"directory first.[/red]"
        )
        sys.exit(1)
    profiles = settings.profiles_base.resolve()
    if root == profiles or profiles in root.parents or root in profiles.parents:
        err_console.print(
            f"[red]Refusing to start the sandbox in {cwd}: it would mount {root} "
            f"read-write, which overlaps the profiles dir {profiles} and with it every "
            f"profile's credentials. cd into a project directory first.[/red]"
        )
        sys.exit(1)


def _run_sandbox_supervised(
    profile_dir: Path,
    cwd: Path,
    claude_args: list[str],
    extra_env: dict[str, str],
    forwarding: _Forwarding,
    *,
    vault_ca: Path | None = None,
) -> None:
    """Run the VM as a child while this process serves its bridges.

    The bridges are threads of this process (see bridges.BridgeServer), so they need it
    alive while podman runs, which the exec path cannot do. podman keeps the terminal in
    raw mode, so the TUI behaves the same as the exec path.
    """
    if forwarding.clipboard and shutil.which("wl-paste") is None:
        err_console.print(
            "[red]'wl-paste' not found on host.[/red] Install wl-clipboard (e.g. "
            "dnf install wl-clipboard) or unset CLAUDE_PROFILE_SANDBOX_CLIPBOARD."
        )
        sys.exit(1)
    server = bridges.BridgeServer(_bridge_services(forwarding))
    try:
        argv = _build_sandbox_argv(
            profile_dir,
            cwd,
            claude_args,
            extra_env,
            forwarding,
            bridge=(server.port, server.token),
            vault_ca=vault_ca,
        )
        before = _host_trust_state(profile_dir, cwd)
        server.start()
        # close_fds=False: podman reads its --env-file through an inherited fd (see
        # _secret_env_file); Python opens every other fd non-inheritable, the bridge
        # server's socket included.
        returncode = _wait_forwarding_signals(
            subprocess.Popen(argv, env=os.environ.copy(), close_fds=False)
        )
    finally:
        server.close()
    _report_host_trust_changes(profile_dir, cwd, before)
    sys.exit(128 - returncode if returncode < 0 else returncode)


def _shown(text: str) -> str:
    """text made safe to print: VM-controlled names could carry markup or ANSI escapes."""
    return escape("".join(char if char.isprintable() else "?" for char in text))


def _mcp_servers_state(servers: object, scope: str) -> dict[str, object]:
    if not isinstance(servers, dict):
        return {}
    return {
        f"MCP server '{_shown(str(name))}' ({scope})": server
        for name, server in servers.items()
    }


def _mcp_project_state(path: str, project: dict[str, Any]) -> dict[str, object]:
    scope = f"project {_shown(path)}"
    state = _mcp_servers_state(project.get("mcpServers"), scope)
    approved = project.get("enabledMcpjsonServers")
    if isinstance(approved, list):
        for name in approved:
            state[f"enabledMcpjsonServers '{_shown(str(name))}' ({scope})"] = True
    if project.get("enableAllProjectMcpServers") is True:
        state[f"enableAllProjectMcpServers ({scope})"] = True
    return state


def _host_trust_state(profile_dir: Path, cwd: Path) -> dict[str, object]:
    """What the host would run from files the VM can write, keyed by a description.

    Covers the MCP servers in the profile's .claude.json, the project approvals that
    start .mcp.json servers without asking, and which .claude/settings.local.json files
    claude reads for cwd exist. Only what adds or changes a command counts: claude itself
    writes empty approval lists, and removing a server runs nothing.
    """
    state: dict[str, object] = {}
    try:
        data = json.loads((profile_dir / ".claude.json").read_bytes())
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        state.update(_mcp_servers_state(data.get("mcpServers"), "user scope"))
        projects = data.get("projects")
        for path, project in projects.items() if isinstance(projects, dict) else []:
            if isinstance(project, dict):
                state.update(_mcp_project_state(str(path), project))
    for local in _local_settings_paths(_sandbox_work_root(cwd), cwd):
        if os.path.lexists(local):
            state[escape(str(local))] = True
    return state


def _report_host_trust_changes(
    profile_dir: Path, cwd: Path, before: dict[str, object]
) -> None:
    """Warn about what the session added or changed that claude will run on the host.

    These stay writable from the VM: copies of .claude.json would lose the session's
    own state, and a .claude/settings.local.json that did not exist had nothing to copy.
    """
    after = _host_trust_state(profile_dir, cwd)
    changed = [key for key, value in after.items() if before.get(key) != value]
    if not changed:
        return
    listing = "".join(f"\n  {key}" for key in changed)
    err_console.print(
        f"[yellow]Warning: the sandboxed session added or changed what claude runs on the "
        f"host:{listing}\nThe MCP entries are in {profile_dir / '.claude.json'}. Check "
        f"these before running claude on the host with this profile or in this "
        f"project.[/yellow]"
    )


def _wait_forwarding_signals(proc: subprocess.Popen[bytes]) -> int:
    """Wait for proc, passing SIGTERM and SIGHUP on to it rather than dying from them.

    Both signals kill this process outright by default, and the bridges die with it
    while podman, which a plain ``kill`` of this process does not reach, keeps the VM
    running without them. Forwarding them lets podman stop the VM and exit normally, so
    the session ends cleanly and the caller's ``finally`` still runs.
    """

    def forward(signum: int, _frame: object) -> None:
        proc.send_signal(signum)

    previous = {
        sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        return proc.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _sandbox_enabled(profile_dir: Path) -> bool:
    """Decide whether to launch in a sandbox.

    The ``CLAUDE_PROFILE_SANDBOX`` env override wins when set; otherwise the
    profile's ``.sandbox`` marker decides.
    """
    if settings.sandbox is not None:
        return settings.sandbox
    return _sandbox_marked(profile_dir)


def _sandbox_marked(profile_dir: Path) -> bool:
    """True when the profile has the .sandbox marker.

    lexists: a marker planted as a dangling link still counts, failing safe.
    """
    return os.path.lexists(profile_dir / SANDBOX_MARKER)


def _sandbox_override_note() -> str | None:
    """What CLAUDE_PROFILE_SANDBOX does to every launch while it is set, or None."""
    if settings.sandbox is None:
        return None
    where = "in a microVM" if settings.sandbox else "on the host"
    return f"CLAUDE_PROFILE_SANDBOX is set, so every profile launches {where} until it is unset."


def _was_sandboxed(profile_dir: Path) -> bool:
    """True when the profile is marked for the sandbox or has been launched in one."""
    return _sandbox_marked(profile_dir) or _sandbox_state_path(profile_dir).is_dir()


def _check_statusline_link(profile_dir: Path) -> None:
    """Warn when a sandboxed profile's statusline.sh is not the global link `add` makes.

    claude runs the statusline command on the host at every host launch of the profile.
    The VM cannot change the global script, which it gets read-only, but it sees the
    profile dir read-write, so it can swap the link for a script of its own.
    """
    link = profile_dir / STATUSLINE_FILE
    if not os.path.lexists(link) or not _was_sandboxed(profile_dir):
        return
    expected = Path.home() / ".claude" / STATUSLINE_FILE
    if link.is_symlink() and Path(os.readlink(link)) == expected:
        return
    err_console.print(
        f"[yellow]Warning: {link} is not the link to {expected} that claude-profile "
        f"creates. A sandboxed session can replace it, and claude runs it on the host. "
        f"Check it, then relink it: ln -sfn {expected} {link}[/yellow]"
    )


def _launch_profile(name: str, claude_args: list[str]) -> None:
    d = settings.profiles_base / name
    if not d.exists():
        err_console.print(
            f"[red]Profile '{name}' does not exist.[/red] "
            f"Create it with: claude-profile add {name}"
        )
        sys.exit(1)
    vault, extra_env = _split_agent_vault(d, _load_profile_env(d))
    vault_run = None
    if vault is not None:
        vault_run = _route_agent_vault(name, vault, claude_args)
        if vault_run is None:
            return  # exec'd `infisical agent-vault run`, which starts this launch again
    if _sandbox_enabled(d):
        _launch_sandbox(d, claude_args, extra_env, vault_run=vault_run)
        return
    _check_statusline_link(d)
    shared, _host_loopback = _shared_settings_args(name, claude_args, sandbox=False)
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(d)
    env.update(extra_env)
    if vault_run is not None and vault_run.session is not None:
        env.update(_agent_vault_host_env(vault_run.session))
    # exec replaces this process - no wrapper in between, which matters for claude's TUI
    # (raw terminal mode, signal handling, etc.). An Agent Vault launch keeps the CLI as
    # claude's parent, which signals pass through (see _exec_agent_vault_run).
    os.execvpe(settings.claude_bin, [settings.claude_bin, *shared, *claude_args], env)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] not in KNOWN_COMMANDS and not args[0].startswith("-"):
        _launch_profile(args[0], args[1:])
    else:
        app()
