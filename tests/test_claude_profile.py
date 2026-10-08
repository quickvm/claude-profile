"""Tests for claude_profile."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Generator, Optional
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

import claude_profile
from claude_profile import browser_bridge_host
from claude_profile import (
    SANDBOX_MARKER,
    SKIP_PERMISSIONS_FLAG,
    _build_sandbox_argv,
    _git_common_dir,
    _launch_profile,
    _parse_env_file,
    _sandbox_image_user,
    _sandbox_mounts,
    app,
    main,
)

runner = CliRunner()

# Captured before the autouse fixture below stubs it out, for the tests that exercise the
# real host-install probe.
_real_host_claude_binary = claude_profile._host_claude_binary


@pytest.fixture()
def profiles_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "profiles"
    monkeypatch.setattr(claude_profile.settings, "profiles_base", base)
    return base


@pytest.fixture()
def fake_home(tmp_path: Path) -> Generator[Path, None, None]:
    home = tmp_path / "home"
    home.mkdir()
    with patch("pathlib.Path.home", return_value=home):
        yield home


@pytest.fixture(autouse=True)
def _reset_sandbox_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the sandbox env overrides off unless a test sets them."""
    # Point the trust anchors at an absent dir so the developer's real host CAs (this
    # runs on machines that have them) never add a mount. Tests for that path set it.
    monkeypatch.setattr(claude_profile, "SANDBOX_CA_ANCHORS", str(tmp_path / "no-ca"))
    monkeypatch.setattr(claude_profile.settings, "sandbox", None)
    monkeypatch.setattr(claude_profile.settings, "sandbox_ssh_agent", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_gpg_agent", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_clipboard", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_chrome", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_gh", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_infisical", "")
    monkeypatch.setattr(claude_profile.settings, "sandbox_pulumi", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_forward_env", "")
    # Default the image-user check to root so launch tests skip the real podman call.
    monkeypatch.setattr(claude_profile, "_sandbox_image_user", lambda: "")
    # Default the host-claude probe to "not a native install" so tests never read (or
    # copy) the developer's real claude binary. Tests for that path set it themselves.
    monkeypatch.setattr(claude_profile, "_host_claude_binary", lambda: None)
    # A podman nobody can find, so a launch that a test forgot to stub fails fast
    # instead of booting a real microVM on the developer's machine.
    monkeypatch.setattr(claude_profile.settings, "podman_bin", "podman-stub-me")
    # Keep launches away from the developer's real claude-profile data dir: its MCP
    # image store would add mounts, and launches write files there.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    # Default the git identity probe to none, so launch tests never read the
    # developer's git config. The identity tests use the git_identity fixture.
    monkeypatch.setattr(claude_profile, "_git_identity_mounts", lambda cwd, signing: [])
    # Likewise the work-tree probe; the tests about it restore the real one.
    monkeypatch.setattr(claude_profile, "_git_toplevel", lambda cwd: None)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_no_base_dir(profiles_base: Path) -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No profiles found" in result.output


def test_list_empty_base_dir(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No profiles found" in result.output


def test_list_unauthenticated_profile(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "work" in result.output
    assert "not authenticated" in result.output


def test_list_authenticated_profile(profiles_base: Path) -> None:
    profile_dir = profiles_base / "work"
    profile_dir.mkdir(parents=True)
    (profile_dir / ".credentials.json").write_text("{}")
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "work" in result.output
    assert "authenticated" in result.output


def test_list_multiple_profiles(profiles_base: Path) -> None:
    (profiles_base / "personal").mkdir(parents=True)
    work = profiles_base / "work"
    work.mkdir(parents=True)
    (work / ".credentials.json").write_text("{}")
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "personal" in result.output
    assert "work" in result.output


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_creates_directory(profiles_base: Path, fake_home: Path) -> None:
    # Answer Y to both prompts
    result = runner.invoke(app, ["add", "work"], input="y\ny\nn\n")
    assert result.exit_code == 0
    assert (profiles_base / "work").is_dir()


def test_add_copies_files_from_claude_dir(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text('{"theme": "dark"}')
    (claude_dir / "CLAUDE.md").write_text("# instructions")

    # Answer n to both link prompts (no global dirs exist)
    runner.invoke(app, ["add", "work"], input="n\nn\nn\n")

    profile = profiles_base / "work"
    assert (profile / "settings.json").read_text() == '{"theme": "dark"}'
    assert (profile / "CLAUDE.md").read_text() == "# instructions"


def test_add_links_statusline_to_global(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    global_statusline = claude_dir / "statusline.sh"
    global_statusline.write_text("#!/bin/sh\necho ok")

    runner.invoke(app, ["add", "work"], input="n\nn\nn\n")

    profile_statusline = profiles_base / "work" / "statusline.sh"
    assert profile_statusline.is_symlink()
    assert profile_statusline.readlink() == global_statusline
    # An edit to the global script is what the profile runs.
    global_statusline.write_text("#!/bin/sh\necho edited")
    assert profile_statusline.read_text() == "#!/bin/sh\necho edited"


def test_add_skips_missing_source_files(profiles_base: Path, fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    result = runner.invoke(app, ["add", "work"], input="n\nn\nn\n")
    assert result.exit_code == 0
    profile = profiles_base / "work"
    assert not (profile / "settings.json").exists()
    # is_symlink too: exists() is False for a dangling link.
    assert not (profile / "statusline.sh").is_symlink()
    assert not (profile / "statusline.sh").exists()
    assert not (profile / "CLAUDE.md").exists()


@pytest.mark.parametrize(
    "name", ["build", "sandbox-cache", "shared-settings.json", "../x", ".hidden"]
)
def test_add_rejects_names_that_cannot_be_launched(
    profiles_base: Path, fake_home: Path, name: str
) -> None:
    # `claude-profile build` runs the build command, so a profile named build could
    # never be launched; path-like names would land outside the profiles dir.
    result = runner.invoke(app, ["add", name], input="n\nn\nn\n")
    assert result.exit_code == 1
    assert not (profiles_base / name).exists()
    assert not (profiles_base.parent / "x").exists()


def test_add_fails_if_profile_exists(profiles_base: Path, fake_home: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["add", "work"])
    assert result.exit_code == 1


def test_add_default_yes_creates_symlinks(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "commands").mkdir()
    (claude_dir / "skills").mkdir()

    result = runner.invoke(app, ["add", "work"], input="\n\n\n")
    assert result.exit_code == 0
    profile = profiles_base / "work"
    assert (profile / "commands").is_symlink()
    assert (profile / "skills").is_symlink()


def test_add_decline_both_creates_isolated_dirs(
    profiles_base: Path, fake_home: Path
) -> None:
    (fake_home / ".claude").mkdir()
    result = runner.invoke(app, ["add", "work"], input="n\nn\nn\n")
    assert result.exit_code == 0
    profile = profiles_base / "work"
    assert (profile / "commands").is_dir()
    assert not (profile / "commands").is_symlink()
    assert (profile / "skills").is_dir()
    assert not (profile / "skills").is_symlink()


def test_add_mixed_link_and_isolate(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "commands").mkdir()

    # Link commands (Y), isolate skills (n)
    result = runner.invoke(app, ["add", "work"], input="y\nn\nn\n")
    assert result.exit_code == 0
    profile = profiles_base / "work"
    assert (profile / "commands").is_symlink()
    assert (profile / "skills").is_dir()
    assert not (profile / "skills").is_symlink()


def test_add_global_dir_absent_skips_symlink(
    profiles_base: Path, fake_home: Path
) -> None:
    (fake_home / ".claude").mkdir()
    # Request link but global dir doesn't exist — should warn and not crash
    result = runner.invoke(app, ["add", "work"], input="y\ny\nn\n")
    assert result.exit_code == 0
    profile = profiles_base / "work"
    # Symlinks not created (global dirs absent)
    assert not (profile / "commands").is_symlink()
    assert not (profile / "skills").is_symlink()


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_deletes_profile(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["remove", "work"], input="y\n")
    assert result.exit_code == 0
    assert not (profiles_base / "work").exists()


def test_remove_aborts_on_no(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["remove", "work"], input="n\n")
    assert result.exit_code == 0
    assert (profiles_base / "work").exists()
    assert "Aborted" in result.output


def test_remove_nonexistent_profile(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    result = runner.invoke(app, ["remove", "ghost"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# links — status table
# ---------------------------------------------------------------------------


def test_links_status_symlinked(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    global_cmd = claude_dir / "commands"
    global_cmd.mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / "commands").symlink_to(global_cmd)

    result = runner.invoke(app, ["links", "work"])
    assert result.exit_code == 0
    assert "linked" in result.output


def test_links_status_isolated(profiles_base: Path, fake_home: Path) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / "commands").mkdir()

    result = runner.invoke(app, ["links", "work"])
    assert result.exit_code == 0
    assert "isolated" in result.output


def test_links_status_not_configured(profiles_base: Path, fake_home: Path) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)

    result = runner.invoke(app, ["links", "work"])
    assert result.exit_code == 0
    assert "not configured" in result.output


def test_links_nonexistent_profile(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    result = runner.invoke(app, ["links", "ghost"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# links --link
# ---------------------------------------------------------------------------


def test_links_link_named_dir_without_global_fails(
    profiles_base: Path, fake_home: Path
) -> None:
    (fake_home / ".claude").mkdir()
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["links", "work", "hooks", "--link"])
    assert result.exit_code == 1
    assert not (profiles_base / "work" / "hooks").exists()


def test_links_link_all(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "commands").mkdir()
    (claude_dir / "skills").mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)

    result = runner.invoke(app, ["links", "work", "--link"])
    assert result.exit_code == 0
    assert (profile / "commands").is_symlink()
    assert (profile / "skills").is_symlink()


def test_links_link_single_dir(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "commands").mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)

    result = runner.invoke(app, ["links", "work", "commands", "--link"])
    assert result.exit_code == 0
    assert (profile / "commands").is_symlink()
    assert not (profile / "skills").exists()


def test_links_link_already_symlinked(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    global_cmd = claude_dir / "commands"
    global_cmd.mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / "commands").symlink_to(global_cmd)

    result = runner.invoke(app, ["links", "work", "commands", "--link"])
    assert result.exit_code == 0
    assert "already linked" in result.output


def test_links_link_on_regular_dir_exits_1(
    profiles_base: Path, fake_home: Path
) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "commands").mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / "commands").mkdir()

    result = runner.invoke(app, ["links", "work", "commands", "--link"])
    assert result.exit_code == 1
    assert "isolated" in result.output or "manually" in result.output


def test_links_link_global_absent_exits_1(profiles_base: Path, fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)

    result = runner.invoke(app, ["links", "work", "commands", "--link"])
    assert result.exit_code == 1


def test_links_link_invalid_dir(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    (profiles_base / "work").mkdir()

    result = runner.invoke(app, ["links", "work", "bogus", "--link"])
    assert result.exit_code == 1
    assert "bogus" in result.output


# ---------------------------------------------------------------------------
# links --unlink
# ---------------------------------------------------------------------------


def test_links_unlink_all(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    global_cmd = claude_dir / "commands"
    global_cmd.mkdir()
    global_skills = claude_dir / "skills"
    global_skills.mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / "commands").symlink_to(global_cmd)
    (profile / "skills").symlink_to(global_skills)

    result = runner.invoke(app, ["links", "work", "--unlink"])
    assert result.exit_code == 0
    assert (profile / "commands").is_dir()
    assert not (profile / "commands").is_symlink()
    assert (profile / "skills").is_dir()
    assert not (profile / "skills").is_symlink()


def test_links_unlink_single_dir(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    global_skills = claude_dir / "skills"
    global_skills.mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / "skills").symlink_to(global_skills)

    result = runner.invoke(app, ["links", "work", "skills", "--unlink"])
    assert result.exit_code == 0
    assert (profile / "skills").is_dir()
    assert not (profile / "skills").is_symlink()


def test_links_unlink_removes_symlink_creates_dir(
    profiles_base: Path, fake_home: Path
) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    global_cmd = claude_dir / "commands"
    global_cmd.mkdir()
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / "commands").symlink_to(global_cmd)

    result = runner.invoke(app, ["links", "work", "commands", "--unlink"])
    assert result.exit_code == 0
    assert (profile / "commands").is_dir()
    assert not (profile / "commands").is_symlink()


def test_links_unlink_on_regular_dir_noop(profiles_base: Path, fake_home: Path) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / "commands").mkdir()

    result = runner.invoke(app, ["links", "work", "commands", "--unlink"])
    assert result.exit_code == 0
    assert "already isolated" in result.output
    assert (profile / "commands").is_dir()


def test_links_unlink_not_configured_creates_dir(
    profiles_base: Path, fake_home: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)

    result = runner.invoke(app, ["links", "work", "commands", "--unlink"])
    assert result.exit_code == 0
    assert (profile / "commands").is_dir()


def test_links_unlink_invalid_dir(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    (profiles_base / "work").mkdir()

    result = runner.invoke(app, ["links", "work", "bogus", "--unlink"])
    assert result.exit_code == 1
    assert "bogus" in result.output


# ---------------------------------------------------------------------------
# links --link and --unlink together
# ---------------------------------------------------------------------------


def test_links_link_and_unlink_together_exits_1(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    (profiles_base / "work").mkdir()

    result = runner.invoke(app, ["links", "work", "--link", "--unlink"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# _launch_profile
# ---------------------------------------------------------------------------


def test_launch_sets_config_dir(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    env = mock_exec.call_args[0][2]
    assert env["CLAUDE_CONFIG_DIR"] == str(profiles_base / "work")


def test_launch_passes_args(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", ["--resume", "--debug"])
    _bin, argv, _env = mock_exec.call_args[0]
    assert "--resume" in argv
    assert "--debug" in argv


def test_launch_uses_claude_bin(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    bin_arg, argv, _env = mock_exec.call_args[0]
    assert bin_arg == claude_profile.settings.claude_bin
    assert argv[0] == claude_profile.settings.claude_bin


def test_launch_exits_if_profile_missing(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    with pytest.raises(SystemExit) as exc_info:
        _launch_profile("ghost", [])
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# main dispatch
# ---------------------------------------------------------------------------


def test_main_dispatches_list_to_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["claude-profile", "list"])
    with patch("claude_profile.app") as mock_app:
        main()
    mock_app.assert_called_once()


def test_main_dispatches_unknown_arg_to_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["claude-profile", "work", "--resume"])
    with patch("claude_profile._launch_profile") as mock_launch:
        main()
    mock_launch.assert_called_once_with("work", ["--resume"])


def test_main_treats_flags_as_app_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["claude-profile", "--help"])
    with patch("claude_profile.app") as mock_app:
        main()
    mock_app.assert_called_once()


def test_main_no_args_goes_to_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["claude-profile"])
    with patch("claude_profile.app") as mock_app:
        main()
    mock_app.assert_called_once()


def test_main_dispatches_links_to_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["claude-profile", "links", "work"])
    with patch("claude_profile.app") as mock_app:
        main()
    mock_app.assert_called_once()


def test_main_dispatches_env_to_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["claude-profile", "env", "work"])
    with patch("claude_profile.app") as mock_app:
        main()
    mock_app.assert_called_once()


# ---------------------------------------------------------------------------
# _parse_env_file
# ---------------------------------------------------------------------------


def test_parse_env_file_basic(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nBAZ=qux\n")
    result = _parse_env_file(env_file)
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_skips_comments_and_blanks(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nFOO=bar\n  \n# another\nBAZ=qux\n")
    result = _parse_env_file(env_file)
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_strips_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SINGLE='hello'\nDOUBLE=\"world\"\n")
    result = _parse_env_file(env_file)
    assert result == {"SINGLE": "hello", "DOUBLE": "world"}


def test_parse_env_file_value_with_equals(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=abc=def=ghi\n")
    result = _parse_env_file(env_file)
    assert result == {"TOKEN": "abc=def=ghi"}


def test_parse_env_file_skips_lines_without_equals(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GOOD=value\nBADLINE\n")
    result = _parse_env_file(env_file)
    assert result == {"GOOD": "value"}


# ---------------------------------------------------------------------------
# env subcommand
# ---------------------------------------------------------------------------


def test_env_list_empty(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["env", "work"])
    assert result.exit_code == 0
    assert "No environment variables" in result.output


def test_env_set_and_list(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["env", "work", "--set", "TOKEN=abc123"])
    assert result.exit_code == 0
    assert "Updated" in result.output

    result = runner.invoke(app, ["env", "work"])
    assert result.exit_code == 0
    assert "TOKEN" in result.output
    assert "abc123" in result.output


def test_env_set_multiple(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["env", "work", "--set", "A=1", "--set", "B=2"])
    assert result.exit_code == 0
    env_file = profiles_base / "work" / ".env"
    parsed = _parse_env_file(env_file)
    assert parsed == {"A": "1", "B": "2"}


@pytest.mark.parametrize("existing_mode", [None, 0o644])
def test_env_file_is_readable_by_the_user_only(
    profiles_base: Path, existing_mode: int | None
) -> None:
    # .env holds tokens, and the profile dir is world-readable like ~/.claude.
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    env_file = profile / ".env"
    if existing_mode is not None:
        env_file.write_text("A=1\n")
        env_file.chmod(existing_mode)
    result = runner.invoke(app, ["env", "work", "--set", "TOKEN=abc123"])
    assert result.exit_code == 0
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_env_unset(profiles_base: Path) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("A=1\nB=2\n")

    result = runner.invoke(app, ["env", "work", "--unset", "A"])
    assert result.exit_code == 0
    parsed = _parse_env_file(profile / ".env")
    assert parsed == {"B": "2"}


def test_env_unset_nonexistent_warns(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["env", "work", "--unset", "NOPE"])
    assert result.exit_code == 0
    assert "not set" in result.output


def test_env_set_invalid_format(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["env", "work", "--set", "BADFORMAT"])
    assert result.exit_code == 1
    assert "KEY=VALUE" in result.output


@pytest.mark.parametrize("entry", ["=x", " =x", "MY VAR=x", "1ST=x"])
def test_env_set_rejects_names_that_are_not_variables(
    profiles_base: Path, entry: str
) -> None:
    # An empty or malformed name made every later launch of the profile fail.
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["env", "work", "--set", entry])
    assert result.exit_code == 1
    assert not (profiles_base / "work" / ".env").exists()


def test_env_nonexistent_profile(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    result = runner.invoke(app, ["env", "ghost"])
    assert result.exit_code == 1


def test_env_set_overwrites_existing(profiles_base: Path) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("TOKEN=old\n")

    result = runner.invoke(app, ["env", "work", "--set", "TOKEN=new"])
    assert result.exit_code == 0
    parsed = _parse_env_file(profile / ".env")
    assert parsed == {"TOKEN": "new"}


def test_env_value_with_equals_sign(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["env", "work", "--set", "TOKEN=abc=def=ghi"])
    assert result.exit_code == 0
    parsed = _parse_env_file(profiles_base / "work" / ".env")
    assert parsed == {"TOKEN": "abc=def=ghi"}


# ---------------------------------------------------------------------------
# _launch_profile with .env
# ---------------------------------------------------------------------------


def test_launch_loads_env_file(profiles_base: Path) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("BUILDKITE_API_TOKEN=secret123\n")
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    env = mock_exec.call_args[0][2]
    assert env["BUILDKITE_API_TOKEN"] == "secret123"
    assert env["CLAUDE_CONFIG_DIR"] == str(profile)


def test_launch_without_env_file(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Clear any ambient value so the assertion reflects only .env loading.
    monkeypatch.delenv("BUILDKITE_API_TOKEN", raising=False)
    (profiles_base / "work").mkdir(parents=True)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    env = mock_exec.call_args[0][2]
    assert "BUILDKITE_API_TOKEN" not in env


# ---------------------------------------------------------------------------
# build command
# ---------------------------------------------------------------------------


def test_build_invokes_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 0
    cmd = captured["cmd"]
    assert cmd[0] == claude_profile.settings.podman_bin
    assert "build" in cmd
    assert claude_profile.settings.sandbox_image in cmd
    assert any(c.endswith("sandbox/Containerfile") for c in cmd)


def test_build_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> None:
        raise subprocess.CalledProcessError(2, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()


def test_build_podman_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# _sandbox_image_exists
# ---------------------------------------------------------------------------


def test_sandbox_image_exists_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    assert claude_profile._sandbox_image_exists() is True


def test_sandbox_image_exists_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1)
    )
    assert claude_profile._sandbox_image_exists() is False


def test_sandbox_image_exists_no_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_profile._sandbox_image_exists() is False


# ---------------------------------------------------------------------------
# _git_common_dir
# ---------------------------------------------------------------------------


def test_git_common_dir_relative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=".git\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _git_common_dir(tmp_path) == (tmp_path / ".git").resolve()


def test_git_common_dir_not_a_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _git_common_dir(tmp_path) is None


def test_git_common_dir_no_git_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert _git_common_dir(tmp_path) is None


# ---------------------------------------------------------------------------
# _sandbox_mounts
# ---------------------------------------------------------------------------


def test_sandbox_mounts_no_git(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    profile = tmp_path / "prof"
    profile.mkdir()
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    mounts = _sandbox_mounts(profile, cwd)
    assert f"{cwd}:{cwd}:z" in mounts
    assert not any(".git" in spec for spec in mounts)


def test_sandbox_mounts_includes_external_git_dir(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "wt" / "feature"
    cwd.mkdir(parents=True)
    git_dir = tmp_path / "main" / ".git"
    git_dir.mkdir(parents=True)
    profile = tmp_path / "prof"
    profile.mkdir()
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: git_dir)
    mounts = _sandbox_mounts(profile, cwd)
    assert f"{git_dir}:{git_dir}:z" in mounts


def test_sandbox_mounts_skips_git_dir_inside_cwd(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    profile = tmp_path / "prof"
    profile.mkdir()
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: cwd / ".git")
    mounts = _sandbox_mounts(profile, cwd)
    # the git dir rides along on the cwd mount
    assert not any(spec.startswith(f"{cwd / '.git'}:") for spec in mounts)


# ---------------------------------------------------------------------------
# ancestor .mcp.json (project-scoped MCP servers declared above the CWD)
# ---------------------------------------------------------------------------


def test_ancestor_mcp_json_finds_files_above_cwd(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{}")
    cwd = tmp_path / "src" / "proj"
    cwd.mkdir(parents=True)
    assert tmp_path / ".mcp.json" in claude_profile._ancestor_mcp_json(cwd)


def test_ancestor_mcp_json_skips_cwd_own_file(tmp_path: Path) -> None:
    """The CWD is already bind-mounted, so its own .mcp.json needs no extra mount."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".mcp.json").write_text("{}")
    assert cwd / ".mcp.json" not in claude_profile._ancestor_mcp_json(cwd)


def test_mcp_json_mounts_are_read_only(tmp_path: Path) -> None:
    config = tmp_path / ".mcp.json"
    config.write_text("{}")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert claude_profile._mcp_json_mounts(cwd) == ["-v", f"{config}:{config}:ro,z"]


def test_mcp_json_mounts_empty_when_absent(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert claude_profile._mcp_json_mounts(cwd) == []


def test_sandbox_mounts_includes_ancestor_mcp_json(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / ".mcp.json"
    config.write_text("{}")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    profile = tmp_path / "prof"
    profile.mkdir()
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    assert f"{config}:{config}:ro,z" in _sandbox_mounts(profile, cwd)


# ---------------------------------------------------------------------------
# host CA trust anchors
# ---------------------------------------------------------------------------


def test_ca_trust_mounts_when_anchors_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    anchors = tmp_path / "anchors"
    anchors.mkdir()
    (anchors / "internal-root.pem").write_text("-----BEGIN CERTIFICATE-----")
    monkeypatch.setattr(claude_profile, "SANDBOX_CA_ANCHORS", str(anchors))
    # Read-only and deliberately unrelabelled: see _ca_trust_mounts.
    assert claude_profile._ca_trust_mounts() == ["-v", f"{anchors}:{anchors}:ro"]


def test_ca_trust_mounts_empty_when_no_anchors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    anchors = tmp_path / "anchors"
    anchors.mkdir()
    monkeypatch.setattr(claude_profile, "SANDBOX_CA_ANCHORS", str(anchors))
    assert claude_profile._ca_trust_mounts() == []


def test_ca_trust_mounts_empty_when_dir_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "SANDBOX_CA_ANCHORS", str(tmp_path / "nope"))
    assert claude_profile._ca_trust_mounts() == []


def test_sandbox_mounts_includes_ca_anchors(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    anchors = tmp_path / "anchors"
    anchors.mkdir()
    (anchors / "internal-root.pem").write_text("-----BEGIN CERTIFICATE-----")
    monkeypatch.setattr(claude_profile, "SANDBOX_CA_ANCHORS", str(anchors))
    cwd = tmp_path / "repo"
    cwd.mkdir()
    profile = tmp_path / "prof"
    profile.mkdir()
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    assert f"{anchors}:{anchors}:ro" in _sandbox_mounts(profile, cwd)


# ---------------------------------------------------------------------------
# host claude binary (sandbox tracks the host's version)
# ---------------------------------------------------------------------------


def _native_install(root: Path, version: str = "2.1.220") -> Path:
    """Create a fake native-installer layout and return the versioned binary."""
    versions = root / "claude" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    binary = versions / version
    binary.write_text(f"#!/bin/sh\necho {version}\n")
    binary.chmod(0o755)
    return binary


def test_host_claude_binary_follows_native_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _native_install(tmp_path)
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(binary)
    monkeypatch.setattr(claude_profile.shutil, "which", lambda b: str(link))
    assert _real_host_claude_binary() == binary


def test_host_claude_binary_none_for_other_install_methods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launcher = tmp_path / "node_modules" / ".bin" / "claude"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env node\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(claude_profile.shutil, "which", lambda b: str(launcher))
    assert _real_host_claude_binary() is None


def test_host_claude_binary_none_when_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_profile.shutil, "which", lambda b: None)
    assert _real_host_claude_binary() is None


def test_sandbox_claude_binary_caches_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _native_install(tmp_path)
    monkeypatch.setattr(claude_profile, "_host_claude_binary", lambda: binary)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cached = claude_profile._sandbox_claude_binary()
    assert cached is not None
    assert cached == tmp_path / "data" / "claude-profile" / "claude" / "2.1.220"
    assert cached.read_text() == binary.read_text()
    assert os.access(cached, os.X_OK)  # must still be executable in the VM


def test_sandbox_claude_binary_reuses_existing_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _native_install(tmp_path)
    monkeypatch.setattr(claude_profile, "_host_claude_binary", lambda: binary)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cached = claude_profile._sandbox_claude_binary()

    def fail_copy(src: object, dst: object) -> None:
        raise AssertionError("re-copied an already cached version")

    # Version dirs are immutable, so a second launch on the same version must not copy.
    monkeypatch.setattr(claude_profile.shutil, "copy", fail_copy)
    assert claude_profile._sandbox_claude_binary() == cached


def test_sandbox_claude_binary_prunes_old_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cache = tmp_path / "data" / "claude-profile" / "claude"
    cache.mkdir(parents=True)
    (cache / "2.1.100").write_text("old")
    gone = subprocess.Popen(["true"])
    gone.wait()
    (cache / f".2.1.100.{gone.pid}.partial").write_text("leftover from a killed launch")
    binary = _native_install(tmp_path)
    monkeypatch.setattr(claude_profile, "_host_claude_binary", lambda: binary)
    claude_profile._sandbox_claude_binary()
    assert sorted(p.name for p in cache.iterdir()) == ["2.1.220"]


def test_sandbox_claude_binary_keeps_a_parallel_launchs_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Worktree sandboxes start together; deleting another launch's half-written copy
    # made it fall back to the image's claude.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cache = tmp_path / "data" / "claude-profile" / "claude"
    cache.mkdir(parents=True)
    in_progress = cache / f".2.1.220.{os.getppid()}.partial"
    in_progress.write_text("still being copied")
    binary = _native_install(tmp_path)
    monkeypatch.setattr(claude_profile, "_host_claude_binary", lambda: binary)
    claude_profile._sandbox_claude_binary()
    assert in_progress.exists()


def test_argv_keeps_autoupdater_when_the_host_claude_is_not_mounted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The copy failed, so the VM runs the image's claude: let it update itself.
    binary = _native_install(tmp_path)
    monkeypatch.setattr(claude_profile, "_host_claude_binary", lambda: binary)
    monkeypatch.setattr(claude_profile, "_sandbox_claude_binary", lambda: None)
    argv = _make_argv(monkeypatch, tmp_path, [])
    assert "DISABLE_AUTOUPDATER=1" not in argv


def test_sandbox_claude_binary_none_without_native_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert claude_profile._sandbox_claude_binary() is None
    assert not (tmp_path / "data").exists()


def test_sandbox_mounts_host_claude_read_only(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _native_install(tmp_path)
    monkeypatch.setattr(claude_profile, "_sandbox_claude_binary", lambda: binary)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    prof = tmp_path / "prof"
    prof.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(prof, cwd)
    assert f"{binary}:{claude_profile.SANDBOX_HOST_CLAUDE}:ro,z" in mounts


# ---------------------------------------------------------------------------
# _sandbox_settings_overlay
# ---------------------------------------------------------------------------


def test_sandbox_settings_overlay_strips_sudo(tmp_path: Path) -> None:
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "deny": [
                        "Bash(sudo *)",
                        "Read(~/.ssh/**)",
                        "Edit(~/.ssh/**)",
                        "Read(~/.aws/**)",
                        "Bash(rm -rf *)",
                        "Read(~/.gnupg/**)",
                    ],
                    "allow": ["Bash(git status*)"],
                },
                "env": {"X": "1"},
            }
        )
    )
    overlay = claude_profile._sandbox_settings_overlay(prof)
    assert overlay is not None
    assert overlay == claude_profile._sandbox_state_dir(prof) / "settings.json"
    deny = json.loads(overlay.read_text())["permissions"]["deny"]
    # sudo + ssh/aws guards stripped inside the VM.
    assert not any(
        r.startswith(("Bash(sudo", "Read(~/.ssh", "Edit(~/.ssh", "Read(~/.aws"))
        for r in deny
    )
    # unrelated denies kept.
    assert "Bash(rm -rf *)" in deny
    assert "Read(~/.gnupg/**)" in deny
    data = json.loads(overlay.read_text())
    assert data["permissions"]["allow"] == ["Bash(git status*)"]  # rest preserved
    assert data["env"] == {"X": "1"}


def test_sandbox_settings_overlay_none_when_no_sudo_deny(tmp_path: Path) -> None:
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Bash(rm -rf *)"]}})
    )
    assert claude_profile._sandbox_settings_overlay(prof) is None


def test_sandbox_settings_overlay_warns_when_it_cannot_be_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Bash(sudo *)"]}})
    )
    (
        claude_profile._sandbox_state_dir(prof) / "settings.json"
    ).mkdir()  # writing over a directory fails
    assert claude_profile._sandbox_settings_overlay(prof) is None
    assert "deny rules" in " ".join(capsys.readouterr().err.split())


def test_sandbox_settings_overlay_none_when_no_settings(tmp_path: Path) -> None:
    prof = tmp_path / "prof"
    prof.mkdir()
    assert claude_profile._sandbox_settings_overlay(prof) is None


def test_sandbox_mounts_adds_settings_overlay(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Bash(sudo *)"]}})
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    mounts = _sandbox_mounts(prof, cwd)
    overlay = claude_profile._sandbox_state_dir(prof) / "settings.json"
    assert f"{overlay}:{claude_profile.SANDBOX_CONFIG_DIR}/settings.json:z" in mounts


# ---------------------------------------------------------------------------
# _build_sandbox_argv
# ---------------------------------------------------------------------------


def _make_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    claude_args: list[str],
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir(exist_ok=True)
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    return _build_sandbox_argv(profile, cwd, claude_args, extra_env or {})


def test_argv_has_krun_runtime_and_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _make_argv(monkeypatch, tmp_path, [])
    assert argv[0] == claude_profile.settings.podman_bin
    assert "run" in argv
    assert "run.oci.handler=krun" in argv
    assert "krun.use_passt=1" in argv  # real guest netstack (not TSI)
    assert claude_profile.settings.sandbox_image in argv
    assert "claude" in argv


def test_argv_disables_autoupdater_when_host_claude_mounted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _native_install(tmp_path)
    monkeypatch.setattr(claude_profile, "_host_claude_binary", lambda: binary)
    monkeypatch.setattr(claude_profile, "_sandbox_claude_binary", lambda: binary)
    argv = _make_argv(monkeypatch, tmp_path, [])
    assert "DISABLE_AUTOUPDATER=1" in argv


def test_argv_keeps_autoupdater_without_host_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _make_argv(monkeypatch, tmp_path, [])
    assert "DISABLE_AUTOUPDATER=1" not in argv


def test_argv_sizing_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_ram_mib", 8192)
    monkeypatch.setattr(claude_profile.settings, "sandbox_cpus", 6)
    argv = _make_argv(monkeypatch, tmp_path, [])
    assert "krun.ram_mib=8192" in argv
    assert "krun.cpus=6" in argv


def test_argv_auto_adds_skip_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _make_argv(monkeypatch, tmp_path, ["--resume"])
    assert argv.count(SKIP_PERMISSIONS_FLAG) == 1
    assert "--resume" in argv


def test_argv_does_not_duplicate_skip_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _make_argv(monkeypatch, tmp_path, [SKIP_PERMISSIONS_FLAG])
    assert argv.count(SKIP_PERMISSIONS_FLAG) == 1


def test_argv_skip_permissions_opt_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_skip_permissions", False)
    argv = _make_argv(monkeypatch, tmp_path, [])
    assert SKIP_PERMISSIONS_FLAG not in argv


def _env_file_text(argv: list[str]) -> str:
    """What podman reads from the argv's --env-file, read the way podman opens it."""
    return Path(argv[argv.index("--env-file") + 1]).read_text()


def test_argv_passes_env_vars_off_the_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # podman's argv is readable by every local user in /proc/<pid>/cmdline.
    argv = _make_argv(monkeypatch, tmp_path, [], {"ANTHROPIC_API_KEY": "sk-test"})
    assert not any("sk-test" in arg for arg in argv)
    assert _env_file_text(argv) == "ANTHROPIC_API_KEY=sk-test\n"


def test_argv_env_file_has_no_path_and_survives_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _make_argv(monkeypatch, tmp_path, [], {"TOKEN": "a b=c # d"})
    env_file = argv[argv.index("--env-file") + 1]
    assert os.readlink(env_file).endswith("(deleted)")
    # podman opens it after exec, so a child process must be able to read it too.
    child = subprocess.run(["cat", env_file], close_fds=False, capture_output=True)
    assert child.stdout == b"TOKEN=a b=c # d\n"


def test_argv_env_value_with_newline_fails_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # podman reads one variable per line, so a newline would split the value.
    with pytest.raises(SystemExit):
        _make_argv(monkeypatch, tmp_path, [], {"TOKEN": "line1\nsecret-tail"})
    err = capsys.readouterr().err
    assert "TOKEN" in err
    assert "secret-tail" not in err


_REAL_GIT_IDENTITY_MOUNTS = claude_profile._git_identity_mounts


@pytest.fixture()
def git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run the real git identity probe against an isolated global git config."""
    monkeypatch.setattr(
        claude_profile, "_git_identity_mounts", _REAL_GIT_IDENTITY_MOUNTS
    )
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    global_config = tmp_path / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    return global_config


def _vm_git_config(argv: list[str]) -> Optional[Path]:
    """Host path of the file the VM's git reads as its global config, if any."""
    prefix = "GIT_CONFIG_GLOBAL="
    guest = next((a.removeprefix(prefix) for a in argv if a.startswith(prefix)), None)
    if guest is None:
        return None
    return next(
        Path(argv[i + 1].split(":")[0])
        for i, arg in enumerate(argv)
        if arg == "-v" and argv[i + 1].split(":")[1] == guest
    )


def _git_get(key: str, config: Path, repo: Optional[Path] = None) -> Optional[str]:
    """What the VM's git would resolve for key, given its global config file."""
    cmd = ["git", *(["-C", str(repo)] if repo else []), "config", "--get", key]
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(config)}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.stdout.strip() if result.returncode == 0 else None


def _repo_with_work_identity(
    git_identity: Path, tmp_path: Path, extra: str = ""
) -> Path:
    """A repo whose identity and signing key come from an includeIf file."""
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    work_config = tmp_path / "gitconfig-work"
    work_config.write_text(
        "[user]\n\temail = alice@corp.example\n\tsigningkey = 0xC0FFEE\n" + extra
    )
    git_identity.write_text(
        "[user]\n\tname = Alice Example\n\temail = alice@home.example\n"
        f'[includeIf "gitdir:{repo}/"]\n\tpath = {work_config}\n'
        "[commit]\n\tgpgsign = true\n"
    )
    return repo


def test_argv_carries_the_git_identity_resolved_for_the_cwd(
    git_identity: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Only ~/.gitconfig is mounted, so identity it pulls in through includeIf must be
    # resolved on the host or sandbox commits get the default identity.
    _repo_with_work_identity(git_identity, tmp_path)
    config = _vm_git_config(_make_argv(monkeypatch, tmp_path, []))
    assert config is not None
    assert _git_get("user.name", config) == "Alice Example"
    assert _git_get("user.email", config) == "alice@corp.example"
    # Signing settings only travel when GPG is forwarded.
    assert _git_get("user.signingkey", config) is None


def test_git_identity_includes_signing_when_gpg_is_forwarded(
    git_identity: Path, tmp_path: Path
) -> None:
    repo = _repo_with_work_identity(git_identity, tmp_path)
    mounts = claude_profile._git_identity_mounts(repo, signing=True)
    config = _vm_git_config(mounts)
    assert config is not None
    assert _git_get("user.signingkey", config) == "0xC0FFEE"
    assert _git_get("commit.gpgsign", config) == "true"


def test_git_identity_skips_signing_that_cannot_work_in_the_vm(
    git_identity: Path, tmp_path: Path
) -> None:
    # An SSH signing key file lives on the host; forwarding the setup only breaks commits.
    repo = _repo_with_work_identity(git_identity, tmp_path, "[gpg]\n\tformat = ssh\n")
    config = _vm_git_config(claude_profile._git_identity_mounts(repo, signing=True))
    assert config is not None
    assert _git_get("user.email", config) == "alice@corp.example"
    assert _git_get("user.signingkey", config) is None
    assert _git_get("gpg.format", config) is None


def test_git_identity_lets_repo_config_override_it(
    git_identity: Path, tmp_path: Path
) -> None:
    # Passed as global config, so `git config commit.gpgsign false` in a repo still wins.
    repo = _repo_with_work_identity(git_identity, tmp_path)
    config = _vm_git_config(claude_profile._git_identity_mounts(repo, signing=True))
    assert config is not None
    subprocess.run(
        ["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True
    )
    assert _git_get("commit.gpgsign", config, repo) == "false"


def test_argv_without_git_identity_sets_no_git_config(
    git_identity: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _vm_git_config(_make_argv(monkeypatch, tmp_path, [])) is None


def test_argv_without_env_vars_has_no_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert "--env-file" not in _make_argv(monkeypatch, tmp_path, [])


def test_argv_mounts_cwd_at_real_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, [], {})
    assert f"{cwd}:{cwd}:z" in argv
    assert f"{profile}:{claude_profile.SANDBOX_CONFIG_DIR}:z" in argv
    assert "-w" in argv
    assert str(cwd) in argv


# ---------------------------------------------------------------------------
# add --sandbox
# ---------------------------------------------------------------------------


def test_add_sandbox_creates_marker(
    profiles_base: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_home / ".claude").mkdir()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    result = runner.invoke(app, ["add", "work", "--sandbox"], input="n\nn\nn\n")
    assert result.exit_code == 0
    assert (profiles_base / "work" / SANDBOX_MARKER).exists()


def test_add_without_sandbox_no_marker(profiles_base: Path, fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    result = runner.invoke(app, ["add", "work"], input="n\nn\nn\n")
    assert result.exit_code == 0
    assert not (profiles_base / "work" / SANDBOX_MARKER).exists()


def test_add_sandbox_hints_build_when_image_absent(
    profiles_base: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_home / ".claude").mkdir()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: False)
    result = runner.invoke(app, ["add", "work", "--sandbox"], input="n\nn\nn\n")
    assert result.exit_code == 0
    assert "claude-profile build" in result.output


# ---------------------------------------------------------------------------
# list sandbox indicator
# ---------------------------------------------------------------------------


def test_list_shows_sandbox_indicator(profiles_base: Path) -> None:
    boxed = profiles_base / "boxed"
    boxed.mkdir(parents=True)
    (boxed / SANDBOX_MARKER).touch()
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "microVM" in result.output


# ---------------------------------------------------------------------------
# _launch_profile sandbox routing
# ---------------------------------------------------------------------------


def test_launch_sandbox_routes_to_podman(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.chdir(tmp_path)
    expected_cwd = Path.cwd()
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", ["--resume"])
    binname, argv, _env = mock_exec.call_args[0]
    assert binname == claude_profile.settings.podman_bin
    assert argv[0] == claude_profile.settings.podman_bin
    assert "run" in argv
    assert claude_profile.settings.sandbox_image in argv
    assert SKIP_PERMISSIONS_FLAG in argv
    assert "--resume" in argv
    assert f"{expected_cwd}:{expected_cwd}:z" in argv


def test_launch_sandbox_passes_profile_env_to_podman(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    (profile / ".env").write_text("ANTHROPIC_API_KEY=sk-xyz\n")
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.chdir(tmp_path)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    _binname, argv, env = mock_exec.call_args[0]
    assert "ANTHROPIC_API_KEY=sk-xyz\n" in _env_file_text(argv)
    assert not any("sk-xyz" in arg for arg in argv)
    # Not podman's own environment either: the VM can write .env, and podman would
    # honour LD_PRELOAD and friends set there.
    assert "sk-xyz" not in env.values()


def _sandbox_profile_in(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, cwd: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.chdir(cwd)


@pytest.mark.parametrize("where", ["home", "root", "above-home"])
def test_launch_sandbox_refuses_to_mount_the_home_dir(
    profiles_base: Path,
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
) -> None:
    # The working directory is mounted read-write, so launching from ~ (or anything
    # above it) would hand the VM ~/.ssh keys and every profile's credentials.
    cwd = {"home": fake_home, "root": Path("/"), "above-home": fake_home.parent}[where]
    _sandbox_profile_in(profiles_base, monkeypatch, cwd)
    with patch("os.execvpe") as mock_exec, pytest.raises(SystemExit) as exc_info:
        _launch_profile("work", [])
    assert exc_info.value.code == 1
    mock_exec.assert_not_called()
    assert "whole home directory" in " ".join(capsys.readouterr().err.split())


def test_launch_sandbox_allows_a_project_dir_under_home(
    profiles_base: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = fake_home / "src" / "project"
    project.mkdir(parents=True)
    _sandbox_profile_in(profiles_base, monkeypatch, project)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    mock_exec.assert_called_once()


_REAL_GIT_TOPLEVEL = claude_profile._git_toplevel


@pytest.fixture()
def real_git_toplevel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the real work-tree probe instead of the autouse stub."""
    monkeypatch.setattr(claude_profile, "_git_toplevel", _REAL_GIT_TOPLEVEL)


def _git(*args: str) -> None:
    """Run git without the developer's config (no identity, no commit signing)."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        env=env,
    )


def test_sandbox_mounts_whole_work_tree_for_a_subdirectory(
    real_git_toplevel: None, tmp_path: Path
) -> None:
    # Mounting only the subdir, but the repo's real .git read-write, showed git every
    # other tracked file as deleted; `git commit -a` in the VM recorded that.
    repo = tmp_path / "repo"
    _git("init", "-q", str(repo))
    sub = repo / "backend"
    sub.mkdir()
    profile = tmp_path / "prof"
    profile.mkdir()
    mounts = _sandbox_mounts(profile, sub)
    assert f"{repo}:{repo}:z" in mounts
    assert not any(spec.startswith(f"{sub}:") for spec in mounts)
    assert not any(spec.startswith(f"{repo / '.git'}:") for spec in mounts)


def test_sandbox_mounts_worktree_and_its_common_dir(
    real_git_toplevel: None, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp_path / "feature"
    _git("-C", str(repo), "worktree", "add", "-q", str(worktree))
    profile = tmp_path / "prof"
    profile.mkdir()
    mounts = _sandbox_mounts(profile, worktree / ".")
    assert f"{worktree}:{worktree}:z" in mounts
    assert f"{repo / '.git'}:{repo / '.git'}:z" in mounts


def test_launch_sandbox_refuses_a_work_tree_rooted_at_home(
    real_git_toplevel: None,
    profiles_base: Path,
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # With a dotfiles repo at ~, the work tree of any directory under it is ~ itself.
    _git("init", "-q", str(fake_home))
    project = fake_home / "src" / "notes"
    project.mkdir(parents=True)
    _sandbox_profile_in(profiles_base, monkeypatch, project)
    with patch("os.execvpe") as mock_exec, pytest.raises(SystemExit):
        _launch_profile("work", [])
    mock_exec.assert_not_called()
    assert "whole home directory" in " ".join(capsys.readouterr().err.split())


def test_launch_sandbox_missing_image_exits(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _launch_profile("work", [])
    assert exc_info.value.code == 1


def test_launch_no_marker_uses_host_claude(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (profiles_base / "work").mkdir(parents=True)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    binname, argv, env = mock_exec.call_args[0]
    assert binname == claude_profile.settings.claude_bin
    assert argv[0] == claude_profile.settings.claude_bin
    assert env["CLAUDE_CONFIG_DIR"] == str(profiles_base / "work")


# ---------------------------------------------------------------------------
# sandbox toggle command
# ---------------------------------------------------------------------------


def test_sandbox_on_creates_marker(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (profiles_base / "work").mkdir(parents=True)
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    result = runner.invoke(app, ["sandbox", "work", "--on"])
    assert result.exit_code == 0
    assert (profiles_base / "work" / SANDBOX_MARKER).exists()


def test_sandbox_off_removes_marker(profiles_base: Path) -> None:
    p = profiles_base / "work"
    p.mkdir(parents=True)
    (p / SANDBOX_MARKER).touch()
    result = runner.invoke(app, ["sandbox", "work", "--off"])
    assert result.exit_code == 0
    assert not (p / SANDBOX_MARKER).exists()


def test_sandbox_off_idempotent_without_marker(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["sandbox", "work", "--off"])
    assert result.exit_code == 0


def test_sandbox_status_shows_state(profiles_base: Path) -> None:
    p = profiles_base / "work"
    p.mkdir(parents=True)
    result = runner.invoke(app, ["sandbox", "work"])
    assert result.exit_code == 0
    assert "host" in result.output
    (p / SANDBOX_MARKER).touch()
    result = runner.invoke(app, ["sandbox", "work"])
    assert "microVM" in result.output


def test_sandbox_toggle_nonexistent_profile(profiles_base: Path) -> None:
    profiles_base.mkdir(parents=True)
    result = runner.invoke(app, ["sandbox", "ghost", "--on"])
    assert result.exit_code == 1


def test_sandbox_on_and_off_exits_1(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["sandbox", "work", "--on", "--off"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# CLAUDE_PROFILE_SANDBOX per-launch override
# ---------------------------------------------------------------------------


def test_sandbox_enabled_uses_marker_when_no_override(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = profiles_base / "work"
    p.mkdir(parents=True)
    monkeypatch.setattr(claude_profile.settings, "sandbox", None)
    assert claude_profile._sandbox_enabled(p) is False
    (p / SANDBOX_MARKER).touch()
    assert claude_profile._sandbox_enabled(p) is True


def test_sandbox_enabled_override_wins(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = profiles_base / "work"
    p.mkdir(parents=True)
    (p / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile.settings, "sandbox", False)
    assert claude_profile._sandbox_enabled(p) is False
    (p / SANDBOX_MARKER).unlink()
    monkeypatch.setattr(claude_profile.settings, "sandbox", True)
    assert claude_profile._sandbox_enabled(p) is True


def test_override_forces_host_despite_marker(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    p = profiles_base / "work"
    p.mkdir(parents=True)
    (p / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile.settings, "sandbox", False)
    monkeypatch.chdir(tmp_path)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    binname, _argv, env = mock_exec.call_args[0]
    assert binname == claude_profile.settings.claude_bin
    assert env["CLAUDE_CONFIG_DIR"] == str(p)


def test_empty_setting_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # `CLAUDE_PROFILE_SANDBOX=` (to clear an exported override) failed every command
    # with a pydantic error at import.
    monkeypatch.setenv("CLAUDE_PROFILE_SANDBOX", "")
    assert claude_profile.Settings().sandbox is None


def test_override_forces_sandbox_without_marker(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    p = profiles_base / "work"
    p.mkdir(parents=True)
    monkeypatch.setattr(claude_profile.settings, "sandbox", True)
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.chdir(tmp_path)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    binname, _argv, _env = mock_exec.call_args[0]
    assert binname == claude_profile.settings.podman_bin


# ---------------------------------------------------------------------------
# linked commands/skills/statusline read-only mounts
# ---------------------------------------------------------------------------


def _linked_specs(mounts: list[str]) -> list[str]:
    """Read-only mount specs other than the pinned .sandbox marker and .env."""
    pinned = (f"/{SANDBOX_MARKER}:ro,z", "/.env:ro,z")
    return [m for m in mounts if m.endswith("ro,z") and not m.endswith(pinned)]


def test_linked_dir_mount_for_symlink(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    global_skills = fake_home / ".claude" / "skills"
    global_skills.mkdir(parents=True)
    (profile / "skills").symlink_to(global_skills)
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert f"{global_skills}:{global_skills}:ro,z" in mounts


def test_linked_dir_isolated_dir_not_mounted(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / "skills").mkdir()  # isolated dir, not a symlink
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert _linked_specs(mounts) == []


def test_linked_dir_broken_symlink_skipped(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / "commands").symlink_to(fake_home / ".claude" / "commands")
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert _linked_specs(mounts) == []


def test_linked_dir_symlink_chain_mounts_real_at_target(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    intermediate = fake_home / ".claude" / "skills"
    intermediate.parent.mkdir()
    intermediate.symlink_to(canonical)
    (profile / "skills").symlink_to(intermediate)
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    # real content mounted at the link's immediate (absolute) target
    assert f"{canonical}:{intermediate}:ro,z" in mounts


def test_linked_statusline_mounted_at_link_target(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    global_statusline = fake_home / ".claude" / "statusline.sh"
    global_statusline.parent.mkdir()
    global_statusline.write_text("#!/bin/sh\necho ok")
    (profile / "statusline.sh").symlink_to(global_statusline)
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert f"{global_statusline}:{global_statusline}:ro,z" in mounts


@pytest.mark.parametrize(
    ("name", "planted"),
    [("skills", ".ssh"), ("statusline.sh", ".config/gh/hosts.yml")],
)
def test_linked_mounts_skip_link_retargeted_off_global(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    planted: str,
) -> None:
    # The profile dir is writable from inside the VM, so an agent can repoint a link at
    # any host path. The next launch must not mount that path into the VM.
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    secret = fake_home / planted
    secret.parent.mkdir(parents=True, exist_ok=True)
    if name == "skills":
        secret.mkdir()
    else:
        secret.write_text("oauth_token: x")
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / name).symlink_to(secret)
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert not any(str(secret) in m for m in mounts)


def test_linked_hooks_dir_mounted_at_link_target(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A guard hook written as ~/.claude/hooks/guard.sh resolves to the profile in the
    # VM; unless the link resolves there it exits 127 and the guard fails open.
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    global_hooks = fake_home / ".claude" / "hooks"
    global_hooks.mkdir(parents=True)
    (profile / "hooks").symlink_to(global_hooks)
    cwd = tmp_path / "work"
    cwd.mkdir()
    assert f"{global_hooks}:{global_hooks}:ro,z" in _sandbox_mounts(profile, cwd)


def test_copied_statusline_not_mounted(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / "statusline.sh").write_text("#!/bin/sh\necho own copy")
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert _linked_specs(mounts) == []


def test_sandbox_mounts_includes_gitconfig(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    (fake_home / ".gitconfig").write_text("[user]\n  name = x\n")
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert f"{fake_home / '.gitconfig'}:/home/appuser/.gitconfig:ro,z" in mounts


def test_sandbox_mounts_no_gitconfig(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert not any(".gitconfig" in m for m in mounts)


# ---------------------------------------------------------------------------
# agent forwarding (SSH + GPG)
# ---------------------------------------------------------------------------


def test_free_tcp_port_returns_usable_port() -> None:
    port = claude_profile._free_tcp_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535


def test_ssh_agent_sockets_includes_auth_and_1password(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth = tmp_path / "agent.sock"
    auth.touch()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(auth))
    op_dir = fake_home / ".1password"
    op_dir.mkdir()
    (op_dir / "agent.sock").touch()
    monkeypatch.setattr(
        claude_profile, "_ssh_agent_status", lambda s: 2
    )  # both live w/ keys
    assert claude_profile._ssh_agent_sockets() == [auth, op_dir / "agent.sock"]


def test_ssh_agent_sockets_empty_when_none(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert claude_profile._ssh_agent_sockets() == []


def test_ssh_agent_sockets_skips_missing(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "nope.sock"))
    assert claude_profile._ssh_agent_sockets() == []


def test_ssh_agent_status_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    for rc, expected in [(0, 2), (1, 1), (2, 0)]:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, rc=rc, **k: subprocess.CompletedProcess([], rc),
        )
        assert claude_profile._ssh_agent_status(Path("/x")) == expected


def test_ssh_agent_status_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_profile._ssh_agent_status(Path("/x")) == 0


def test_ssh_agent_sockets_skips_dead(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gnome = fake_home / "gnome.sock"
    gnome.touch()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(gnome))
    op = fake_home / ".1password" / "agent.sock"
    op.parent.mkdir()
    op.touch()
    # gnome is a dead stale stub; 1password has keys
    monkeypatch.setattr(
        claude_profile, "_ssh_agent_status", lambda s: 0 if s == gnome else 2
    )
    assert claude_profile._ssh_agent_sockets() == [op]


def test_ssh_agent_sockets_prefers_keyed_over_empty(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gnome = fake_home / "gnome.sock"
    gnome.touch()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(gnome))
    op = fake_home / ".1password" / "agent.sock"
    op.parent.mkdir()
    op.touch()
    # gnome live-but-empty; 1password has keys -> 1password ordered first
    monkeypatch.setattr(
        claude_profile, "_ssh_agent_status", lambda s: 1 if s == gnome else 2
    )
    assert claude_profile._ssh_agent_sockets() == [op, gnome]


def test_forwarding_env_empty() -> None:
    assert claude_profile._forwarding_env(None) == []
    assert claude_profile._forwarding_env(claude_profile._Forwarding([])) == []


def test_forwarding_env_ssh(tmp_path: Path) -> None:
    a = tmp_path / "a.sock"
    fwd = claude_profile._Forwarding([(a, a, 1111)], ssh_auth_sock=a)
    env = claude_profile._forwarding_env(fwd)
    assert f"CLAUDE_SANDBOX_FORWARDS={a}=1111" in env
    assert f"SSH_AUTH_SOCK={a}" in env
    assert "GNUPGHOME=/home/appuser/.gnupg" not in env


def test_forwarding_env_gpg(tmp_path: Path) -> None:
    host = tmp_path / "S.gpg-agent.extra"
    guest = Path("/home/appuser/.gnupg/S.gpg-agent")
    fwd = claude_profile._Forwarding([(host, guest, 2222)], gpg_pubkeys=b"ABC")
    env = claude_profile._forwarding_env(fwd)
    assert f"CLAUDE_SANDBOX_FORWARDS={guest}=2222" in env
    assert "GNUPGHOME=/home/appuser/.gnupg" in env
    assert (
        f"CLAUDE_SANDBOX_GPG_PUBKEYS_FILE={claude_profile.SANDBOX_GPG_PUBKEYS}" in env
    )


def test_argv_gpg_pubkeys_reach_the_vm_as_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Linux caps a single argv or env string at 128 KiB (MAX_ARG_STRLEN); a public
    # keyring past that, base64'd into one variable, failed the launch with E2BIG.
    keyring = os.urandom(200 * 1024)

    def fake_export(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd == ["gpg", "--export"]
        return subprocess.CompletedProcess(cmd, 0, stdout=keyring)

    extra = tmp_path / "S.gpg-agent.extra"
    extra.touch()
    monkeypatch.setattr(claude_profile.settings, "sandbox_gpg_agent", True)
    monkeypatch.setattr(claude_profile, "_gpg_extra_socket", lambda: extra)
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: 6000)
    with patch("subprocess.run", fake_export):
        fwd = claude_profile._build_forwarding()
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile, cwd = tmp_path / "prof", tmp_path / "work"
    profile.mkdir()
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, [], {}, fwd)
    assert max(len(arg) for arg in argv) < 128 * 1024
    prefix = "CLAUDE_SANDBOX_GPG_PUBKEYS_FILE="
    guest_path = next(a for a in argv if a.startswith(prefix)).removeprefix(prefix)
    source = next(
        argv[i + 1].split(":")[0]
        for i, arg in enumerate(argv)
        if arg == "-v" and argv[i + 1].split(":")[1] == guest_path
    )
    assert Path(source).read_bytes() == keyring


def test_gpg_extra_socket_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="/nope/S.gpg-agent.extra\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._gpg_extra_socket() is None


def test_gpg_extra_socket_launches_idle_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extra = tmp_path / "S.gpg-agent.extra"

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if "--launch" in cmd:
            extra.touch()  # the agent binds its sockets on launch
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{extra}\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._gpg_extra_socket() == extra


def test_gpg_extra_socket_no_gpgconf(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_profile._gpg_extra_socket() is None


def test_export_gpg_pubkeys(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, stdout=b"PUBKEYBYTES")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._export_gpg_pubkeys() == b"PUBKEYBYTES"


def test_export_gpg_pubkeys_none_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._export_gpg_pubkeys() is None


def test_build_forwarding_ssh_only(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth = tmp_path / "agent.sock"
    auth.touch()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(auth))
    monkeypatch.setattr(claude_profile.settings, "sandbox_ssh_agent", True)
    monkeypatch.setattr(
        claude_profile, "_ssh_agent_status", lambda s: 2
    )  # live w/ keys
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: 5000)
    fwd = claude_profile._build_forwarding()
    guest = Path(claude_profile.SANDBOX_AGENT_DIR) / "ssh-agent-0.sock"
    assert fwd.forwards == [(auth, guest, 5000)]
    assert fwd.ssh_auth_sock == guest
    assert fwd.gpg_pubkeys is None


def test_build_forwarding_ssh_guest_path_stays_out_of_run_user(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The entrypoint chowns each guest socket's parent dir. If that is /run/user/<uid>,
    # gpg moves its socket dir under it and never reaches the GPG bridge.
    auth = Path(f"/run/user/{os.getuid()}/ssh-agent.socket")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(auth))
    monkeypatch.setattr(claude_profile.settings, "sandbox_ssh_agent", True)
    monkeypatch.setattr(claude_profile, "_ssh_agent_sockets", lambda: [auth])
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: 5000)
    fwd = claude_profile._build_forwarding()
    guests = [guest for _host, guest, _port in fwd.forwards]
    assert guests and not any(str(g).startswith("/run/user/") for g in guests)


def test_build_forwarding_gpg_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extra = tmp_path / "S.gpg-agent.extra"
    extra.touch()
    monkeypatch.setattr(claude_profile.settings, "sandbox_gpg_agent", True)
    monkeypatch.setattr(claude_profile, "_gpg_extra_socket", lambda: extra)
    monkeypatch.setattr(claude_profile, "_export_gpg_pubkeys", lambda: b"ABC")
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: 6000)
    fwd = claude_profile._build_forwarding()
    guest = Path(claude_profile.SANDBOX_GNUPGHOME) / "S.gpg-agent"
    assert fwd.forwards == [(extra, guest, 6000)]
    assert fwd.ssh_auth_sock is None
    assert fwd.gpg_pubkeys == b"ABC"


def test_build_forwarding_gpg_warns_when_agent_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_gpg_agent", True)
    monkeypatch.setattr(claude_profile, "_gpg_extra_socket", lambda: None)
    fwd = claude_profile._build_forwarding()
    assert fwd.forwards == []
    assert fwd.gpg_pubkeys is None
    assert "gpg-agent" in capsys.readouterr().err


def test_build_forwarding_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fwd = claude_profile._build_forwarding()
    assert fwd.forwards == []
    assert fwd.ssh_auth_sock is None
    assert fwd.gpg_pubkeys is None


def test_argv_forwarding_adds_pasta_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    a = tmp_path / "a.sock"
    fwd = claude_profile._Forwarding([(a, a, 1234)], ssh_auth_sock=a)
    argv = _build_sandbox_argv(profile, cwd, [], {}, fwd)
    assert any(arg.startswith("--network=pasta") for arg in argv)
    assert f"SSH_AUTH_SOCK={a}" in argv
    assert f"CLAUDE_SANDBOX_FORWARDS={a}=1234" in argv


def test_argv_no_forwarding_no_pasta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, [], {})
    assert not any(arg.startswith("--network=pasta") for arg in argv)
    assert not any(arg.startswith("CLAUDE_SANDBOX_FORWARDS") for arg in argv)


def test_launch_supervised_when_forwarding(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    sock = Path("/run/x.sock")
    fwd = claude_profile._Forwarding([(sock, sock, 1234)], ssh_auth_sock=sock)
    monkeypatch.setattr(claude_profile, "_build_forwarding", lambda: fwd)
    monkeypatch.setattr(claude_profile.shutil, "which", lambda _: "/usr/bin/socat")
    started: list[tuple[Path, int]] = []

    def fake_bridge(host: Path, port: int) -> Mock:
        started.append((host, port))
        return Mock()

    monkeypatch.setattr(claude_profile, "_start_host_bridge", fake_bridge)
    monkeypatch.chdir(tmp_path)
    with (
        patch("subprocess.Popen") as popen,
        patch("os.execvpe") as mock_exec,
        pytest.raises(SystemExit) as exc_info,
    ):
        popen.return_value.wait.return_value = 0
        _launch_profile("work", [])
    assert exc_info.value.code == 0
    mock_exec.assert_not_called()
    popen.assert_called_once()
    assert any(arg.startswith("--network=pasta") for arg in popen.call_args[0][0])
    assert started == [(sock, 1234)]


SUPERVISOR_HARNESS = textwrap.dedent(
    """
    import pathlib, shutil, subprocess, sys
    import claude_profile

    bridge_pid, vm_pid = sys.argv[1], sys.argv[2]

    def fake_bridge(host, port):
        proc = subprocess.Popen(["sleep", "300"])
        pathlib.Path(bridge_pid).write_text(str(proc.pid))
        return proc

    claude_profile._start_host_bridge = fake_bridge
    claude_profile._build_sandbox_argv = lambda *args, **kwargs: [
        "sh", "-c", f"echo $$ > {vm_pid}; exec sleep 300"
    ]
    shutil.which = lambda name: "/usr/bin/" + name
    sock = pathlib.Path("/run/agent.sock")
    claude_profile._run_sandbox_supervised(
        pathlib.Path("/profile"),
        pathlib.Path("/cwd"),
        [],
        {},
        claude_profile._Forwarding([(sock, sock, 1)]),
    )
    """
)


def _proc_state(pid: int) -> str:
    """The /proc state letter of pid, or "" once it is gone."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return ""


def _catches(pid: int, signum: int) -> bool:
    """True once pid has installed a handler for signum (its SigCgt mask)."""
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("SigCgt:"):
            return bool(int(line.split()[1], 16) & (1 << (signum - 1)))
    return False


def _poll(condition: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_supervised_launch_leaves_no_bridge_when_argv_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Building the argv can exit (a value with a newline, an unwritable file). Bridges
    # started before that kept relaying the SSH agent to any local process.
    started: list[Mock] = []

    def fake_bridge(host: Path, port: int) -> Mock:
        started.append(Mock())
        return started[-1]

    def failing_argv(*args: object, **kwargs: object) -> list[str]:
        raise SystemExit(1)

    monkeypatch.setattr(claude_profile, "_build_sandbox_argv", failing_argv)
    monkeypatch.setattr(claude_profile.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(claude_profile, "_start_host_bridge", fake_bridge)
    sock = Path("/run/agent.sock")
    with pytest.raises(SystemExit):
        claude_profile._run_sandbox_supervised(
            tmp_path, tmp_path, [], {}, claude_profile._Forwarding([(sock, sock, 1)])
        )
    assert all(bridge.terminate.called for bridge in started)


def test_supervised_launch_lets_podman_read_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # podman opens --env-file /dev/fd/N, which only works if the fd is inherited.
    seen = tmp_path / "seen"
    env_file = claude_profile._secret_env_file({"TOKEN": "x"})
    monkeypatch.setattr(
        claude_profile,
        "_build_sandbox_argv",
        lambda *args, **kwargs: ["sh", "-c", f'cat "{env_file}" > "{seen}"'],
    )
    monkeypatch.setattr(claude_profile.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(claude_profile, "_start_host_bridge", lambda host, port: Mock())
    sock = Path("/run/agent.sock")
    try:
        with pytest.raises(SystemExit) as exc_info:
            claude_profile._run_sandbox_supervised(
                tmp_path,
                tmp_path,
                [],
                {},
                claude_profile._Forwarding([(sock, sock, 1)]),
            )
    finally:
        os.close(int(env_file.rsplit("/", 1)[1]))
    assert exc_info.value.code == 0
    assert seen.read_text() == "TOKEN=x\n"


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_supervised_launch_stops_bridges_on_signal(tmp_path: Path, signum: int) -> None:
    # Closing the terminal sends SIGHUP; `kill` and systemd send SIGTERM. Either must
    # still tear the host bridges down, or they leave the forwarded agents reachable on
    # host ports after the session is gone.
    bridge_pid, vm_pid = tmp_path / "bridge.pid", tmp_path / "vm.pid"
    launcher = subprocess.Popen(
        [sys.executable, "-c", SUPERVISOR_HARNESS, str(bridge_pid), str(vm_pid)]
    )
    try:
        assert _poll(lambda: vm_pid.exists() and vm_pid.read_text().strip(), 10)
        _poll(lambda: _catches(launcher.pid, signum), 3)
        launcher.send_signal(signum)
        # The VM stand-in dies from the forwarded signal: exit 128+N, as a shell would.
        assert launcher.wait(timeout=10) == 128 + signum
        bridge = int(bridge_pid.read_text())
        assert _poll(lambda: _proc_state(bridge) in ("", "Z"), 5)
    finally:
        launcher.kill()
        for pidfile in (bridge_pid, vm_pid):
            with contextlib.suppress(ValueError, OSError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_launch_agent_no_socat_exits(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    sock = Path("/run/x.sock")
    fwd = claude_profile._Forwarding([(sock, sock, 1234)])
    monkeypatch.setattr(claude_profile, "_build_forwarding", lambda: fwd)
    monkeypatch.setattr(claude_profile.shutil, "which", lambda _: None)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _launch_profile("work", [])
    assert exc_info.value.code == 1


def test_launch_no_forwards_uses_exec(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.setattr(
        claude_profile, "_build_forwarding", lambda: claude_profile._Forwarding([])
    )
    monkeypatch.chdir(tmp_path)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    binname, argv, _env = mock_exec.call_args[0]
    assert binname == claude_profile.settings.podman_bin
    assert not any(arg.startswith("--network=pasta") for arg in argv)


# ---------------------------------------------------------------------------
# clipboard bridge
# ---------------------------------------------------------------------------


def test_build_forwarding_clipboard_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_clipboard", True)
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: 7777)
    fwd = claude_profile._build_forwarding()
    assert fwd.forwards == []
    assert fwd.clipboard_port == 7777
    assert fwd.active() is True


def test_build_forwarding_no_clipboard_when_disabled() -> None:
    fwd = claude_profile._build_forwarding()
    assert fwd.clipboard_port is None
    assert fwd.active() is False


def test_forwarding_env_clipboard_only() -> None:
    fwd = claude_profile._Forwarding([], clipboard_port=7777)
    env = claude_profile._forwarding_env(fwd)
    assert f"{claude_profile.SANDBOX_CLIPBOARD_PORT_ENV}=7777" in env
    # No agent sockets, so no CLAUDE_SANDBOX_FORWARDS entry is emitted.
    assert not any(e.startswith("CLAUDE_SANDBOX_FORWARDS") for e in env)


def test_argv_clipboard_adds_pasta_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    fwd = claude_profile._Forwarding([], clipboard_port=7777)
    argv = _build_sandbox_argv(profile, cwd, [], {}, fwd)
    assert any(arg.startswith("--network=pasta") for arg in argv)
    assert f"{claude_profile.SANDBOX_CLIPBOARD_PORT_ENV}=7777" in argv


def test_clipboard_host_handler_is_packaged() -> None:
    handler = claude_profile._clipboard_host_handler()
    assert handler.name == "clipboard_host.sh"
    assert handler.exists()


def test_launch_supervised_clipboard_only(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    fwd = claude_profile._Forwarding([], clipboard_port=7777)
    monkeypatch.setattr(claude_profile, "_build_forwarding", lambda: fwd)
    monkeypatch.setattr(claude_profile.shutil, "which", lambda _: "/usr/bin/tool")
    host_bridges: list[tuple[Path, int]] = []

    def fake_host_bridge(host: Path, port: int) -> Mock:
        host_bridges.append((host, port))
        return Mock()

    clip_ports: list[int] = []

    def fake_clip_bridge(port: int) -> Mock:
        clip_ports.append(port)
        return Mock()

    monkeypatch.setattr(claude_profile, "_start_host_bridge", fake_host_bridge)
    monkeypatch.setattr(
        claude_profile, "_start_clipboard_host_bridge", fake_clip_bridge
    )
    monkeypatch.chdir(tmp_path)
    with (
        patch("subprocess.Popen") as popen,
        patch("os.execvpe") as mock_exec,
        pytest.raises(SystemExit) as exc_info,
    ):
        popen.return_value.wait.return_value = 0
        _launch_profile("work", [])
    assert exc_info.value.code == 0
    mock_exec.assert_not_called()
    popen.assert_called_once()
    assert any(arg.startswith("--network=pasta") for arg in popen.call_args[0][0])
    assert clip_ports == [7777]
    assert host_bridges == []


def test_launch_clipboard_no_wl_paste_exits(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    fwd = claude_profile._Forwarding([], clipboard_port=7777)
    monkeypatch.setattr(claude_profile, "_build_forwarding", lambda: fwd)
    # socat present on host, wl-paste absent.
    monkeypatch.setattr(
        claude_profile.shutil,
        "which",
        lambda name: None if name == "wl-paste" else "/usr/bin/socat",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _launch_profile("work", [])
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Claude in Chrome bridge
# ---------------------------------------------------------------------------


def test_browser_bridge_live_true_with_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import socket as _socket

    d = tmp_path / "bridge"
    d.mkdir()
    monkeypatch.setattr(claude_profile, "_browser_bridge_dir", lambda: d)
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(str(d / "123.sock"))
    srv.listen(1)
    try:
        assert claude_profile._browser_bridge_live() is True
    finally:
        srv.close()


def test_browser_bridge_live_false_stale_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import socket as _socket

    d = tmp_path / "bridge"
    d.mkdir()
    monkeypatch.setattr(claude_profile, "_browser_bridge_dir", lambda: d)
    # Bound then closed without listen(): the socket file remains but connect is
    # refused — a stale native-host socket left behind after a crash.
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(str(d / "456.sock"))
    srv.close()
    assert claude_profile._browser_bridge_live() is False


def test_browser_bridge_live_false_missing_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        claude_profile, "_browser_bridge_dir", lambda: tmp_path / "nope"
    )
    assert claude_profile._browser_bridge_live() is False


def test_build_forwarding_chrome_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_chrome", True)
    monkeypatch.setattr(claude_profile, "_browser_bridge_live", lambda: True)
    ports = iter([8888, 9999])
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: next(ports))
    fwd = claude_profile._build_forwarding()
    assert fwd.forwards == []
    assert fwd.browser_port == 8888
    assert fwd.browser_open_port == 9999
    assert fwd.active() is True


def test_build_forwarding_chrome_port_set_even_when_not_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_chrome", True)
    monkeypatch.setattr(claude_profile, "_browser_bridge_live", lambda: False)
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: 8888)
    # The guest still presents the socket and reconnects until the host native host
    # appears, so the port is allocated regardless; the warning is informational.
    fwd = claude_profile._build_forwarding()
    assert fwd.browser_port == 8888


def test_build_forwarding_no_chrome_when_disabled() -> None:
    fwd = claude_profile._build_forwarding()
    assert fwd.browser_port is None
    assert fwd.active() is False


def test_forwarding_env_chrome_only() -> None:
    fwd = claude_profile._Forwarding([], browser_port=8888)
    env = claude_profile._forwarding_env(fwd)
    assert f"{claude_profile.SANDBOX_BROWSER_BRIDGE_PORT_ENV}=8888" in env
    assert not any(e.startswith("CLAUDE_SANDBOX_FORWARDS") for e in env)


def test_forwarding_env_browser_open() -> None:
    fwd = claude_profile._Forwarding([], browser_open_port=9999)
    env = claude_profile._forwarding_env(fwd)
    assert f"{claude_profile.SANDBOX_BROWSER_OPEN_PORT_ENV}=9999" in env
    assert fwd.active() is True


def test_argv_appends_chrome_flag_when_sandbox_chrome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.setattr(claude_profile.settings, "sandbox_chrome", True)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, [], {})
    assert "--chrome" in argv


def test_argv_respects_user_no_chrome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.setattr(claude_profile.settings, "sandbox_chrome", True)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, ["--no-chrome"], {})
    assert "--chrome" not in argv


def test_argv_no_chrome_flag_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, [], {})
    assert "--chrome" not in argv


def test_chrome_extension_guest_path_found(fake_home: Path) -> None:
    ext = (
        fake_home
        / ".config"
        / "google-chrome"
        / "Default"
        / "Extensions"
        / claude_profile.CHROME_EXTENSION_ID
    )
    ext.mkdir(parents=True)
    assert claude_profile._chrome_extension_guest_path() == (
        f"/home/appuser/.config/google-chrome/Default/Extensions/"
        f"{claude_profile.CHROME_EXTENSION_ID}"
    )


def test_chrome_extension_guest_path_none_when_absent(fake_home: Path) -> None:
    (fake_home / ".config" / "google-chrome" / "Default").mkdir(parents=True)
    assert claude_profile._chrome_extension_guest_path() is None


def test_chrome_extension_guest_path_finds_numbered_profile(fake_home: Path) -> None:
    ext = (
        fake_home
        / ".config"
        / "chromium"
        / "Profile 2"
        / "Extensions"
        / claude_profile.CHROME_EXTENSION_ID
    )
    ext.mkdir(parents=True)
    assert claude_profile._chrome_extension_guest_path() == (
        f"/home/appuser/.config/chromium/Profile 2/Extensions/"
        f"{claude_profile.CHROME_EXTENSION_ID}"
    )


def test_sandbox_mounts_masks_profile_chrome_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # an in-VM "Install Chrome extension" must not rewrite the host's native-host wrapper
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / "chrome").mkdir()
    (profile / "chrome" / "chrome-native-host").write_text("host wrapper\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert (
        f"{claude_profile._sandbox_state_dir(profile) / 'chrome'}:{claude_profile.SANDBOX_CONFIG_DIR}/chrome:z"
        in mounts
    )
    # the real wrapper is untouched on the host
    assert (profile / "chrome" / "chrome-native-host").read_text() == "host wrapper\n"


def _write_creds(profile: Path, scopes: list[str]) -> None:
    (profile / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "scopes": scopes}})
    )


def test_profile_oauth_scopes_reads_scopes(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    profile.mkdir()
    _write_creds(profile, ["user:inference", "user:profile"])
    assert claude_profile._profile_oauth_scopes(profile) == [
        "user:inference",
        "user:profile",
    ]


def test_profile_oauth_scopes_none_when_missing(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    profile.mkdir()
    assert claude_profile._profile_oauth_scopes(profile) is None


def test_warn_missing_chrome_scope_warns_for_setup_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "personal"
    profile.mkdir()
    _write_creds(profile, ["user:inference"])  # setup-token style
    claude_profile._warn_missing_chrome_scope(profile)
    err = capsys.readouterr().err
    assert "user:profile" in err
    assert "/login" in err


def test_warn_missing_chrome_scope_silent_when_scope_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "personal"
    profile.mkdir()
    _write_creds(profile, ["user:inference", "user:profile"])
    claude_profile._warn_missing_chrome_scope(profile)
    assert capsys.readouterr().err == ""


def test_warn_missing_chrome_scope_silent_without_creds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "personal"
    profile.mkdir()
    claude_profile._warn_missing_chrome_scope(profile)
    assert capsys.readouterr().err == ""


def test_browser_open_host_handler_is_packaged() -> None:
    handler = claude_profile._browser_open_host_handler()
    assert handler.name == "browser_open_host.sh"
    assert handler.exists()


def test_argv_chrome_adds_pasta_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    fwd = claude_profile._Forwarding([], browser_port=8888)
    argv = _build_sandbox_argv(profile, cwd, [], {}, fwd)
    assert any(arg.startswith("--network=pasta") for arg in argv)
    assert f"{claude_profile.SANDBOX_BROWSER_BRIDGE_PORT_ENV}=8888" in argv


def test_browser_bridge_host_handler_is_packaged() -> None:
    handler = claude_profile._browser_bridge_host_handler()
    assert handler.name == "browser_bridge_host.py"
    assert handler.exists()


def test_browser_bridge_write_all_delivers_every_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # os.write may write only part of a buffer; a dropped tail would corrupt the
    # length-prefixed frame stream the in-VM claude reads.
    written: list[bytes] = []

    def short_write(fd: int, data: bytes) -> int:
        written.append(bytes(data[:3]))
        return len(written[-1])

    monkeypatch.setattr(browser_bridge_host.os, "write", short_write)
    browser_bridge_host.write_all(1, b"\x0a\x00\x00\x000123456789")
    assert b"".join(written) == b"\x0a\x00\x00\x000123456789"


@pytest.mark.parametrize(
    ("mode", "found"), [(0o700, True), (0o750, False), (0o755, False)]
)
def test_browser_bridge_uses_only_a_private_socket_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: int, found: bool
) -> None:
    # claude's own client refuses a bridge dir others can write to; the host proxy
    # must too, or another local user can plant a socket the VM's browser calls reach.
    bridge_dir = tmp_path / "claude-mcp-browser-bridge-me"
    bridge_dir.mkdir()
    (bridge_dir / "123.sock").touch()
    bridge_dir.chmod(mode)
    monkeypatch.setattr(browser_bridge_host, "DIR", str(bridge_dir))
    expected = str(bridge_dir / "123.sock") if found else None
    assert browser_bridge_host.newest_sock() == expected


def test_browser_bridge_ignores_a_symlinked_socket_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = tmp_path / "elsewhere"
    private.mkdir(mode=0o700)
    (private / "123.sock").touch()
    link = tmp_path / "claude-mcp-browser-bridge-me"
    link.symlink_to(private)
    monkeypatch.setattr(browser_bridge_host, "DIR", str(link))
    assert browser_bridge_host.newest_sock() is None


def test_launch_supervised_chrome_only(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    fwd = claude_profile._Forwarding([], browser_port=8888, browser_open_port=9999)
    monkeypatch.setattr(claude_profile, "_build_forwarding", lambda: fwd)
    monkeypatch.setattr(claude_profile.shutil, "which", lambda _: "/usr/bin/tool")
    browser_ports: list[int] = []
    browser_open_ports: list[int] = []

    def fake_browser_bridge(port: int) -> Mock:
        browser_ports.append(port)
        return Mock()

    def fake_browser_open_bridge(port: int) -> Mock:
        browser_open_ports.append(port)
        return Mock()

    monkeypatch.setattr(
        claude_profile, "_start_browser_host_bridge", fake_browser_bridge
    )
    monkeypatch.setattr(
        claude_profile, "_start_browser_open_host_bridge", fake_browser_open_bridge
    )
    monkeypatch.chdir(tmp_path)
    with (
        patch("subprocess.Popen") as popen,
        patch("os.execvpe") as mock_exec,
        pytest.raises(SystemExit) as exc_info,
    ):
        popen.return_value.wait.return_value = 0
        _launch_profile("work", [])
    assert exc_info.value.code == 0
    mock_exec.assert_not_called()
    popen.assert_called_once()
    argv = popen.call_args[0][0]
    assert any(arg.startswith("--network=pasta") for arg in argv)
    assert f"{claude_profile.SANDBOX_BROWSER_OPEN_PORT_ENV}=9999" in argv
    assert browser_ports == [8888]
    assert browser_open_ports == [9999]


# ---------------------------------------------------------------------------
# GitHub token (gh)
# ---------------------------------------------------------------------------


def test_gh_token_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="gho_abc123\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._gh_token() == "gho_abc123"


def test_gh_token_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._gh_token() is None


def test_gh_token_none_when_gh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_profile._gh_token() is None


def test_gh_token_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="gh", timeout=10)

    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_profile._gh_token() is None


def test_with_gh_token_disabled_does_not_call_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def spy() -> str:
        nonlocal called
        called = True
        return "gho_x"

    monkeypatch.setattr(claude_profile, "_gh_token", spy)
    assert claude_profile._with_gh_token({"A": "1"}) == {"A": "1"}
    assert called is False


def test_with_gh_token_injects_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_gh", True)
    monkeypatch.setattr(claude_profile, "_gh_token", lambda: "gho_secret")
    assert claude_profile._with_gh_token({"A": "1"}) == {
        "A": "1",
        "GH_TOKEN": "gho_secret",
    }


def test_with_gh_token_warns_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_gh", True)
    monkeypatch.setattr(claude_profile, "_gh_token", lambda: None)
    assert claude_profile._with_gh_token({"A": "1"}) == {"A": "1"}


# ---------------------------------------------------------------------------
# infisical login forwarding
# ---------------------------------------------------------------------------


def _make_jwt(exp: int) -> str:
    """Minimal JWT whose payload carries the given exp (only the payload matters)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{body}.sig"


def _keyring_blob(jwt: str) -> str:
    return json.dumps(
        {"JTWToken": jwt, "RefreshToken": "r", "email": "e@x", "privateKey": "p"}
    )


INFISICAL_USERS = [
    {"email": "joe@quickvm.com", "domain": "https://infisical.quickvm.example/api"},
    {"email": "alice@corp.example", "domain": "https://secrets.corp.example/api"},
    {"email": "alice@home.example", "domain": "https://infisical.home.example/api"},
]


def _setup_infisical(
    monkeypatch: pytest.MonkeyPatch, allowlist: str, tokens: dict[str, str | None]
) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_infisical", allowlist)
    monkeypatch.setattr(
        claude_profile.shutil, "which", lambda _c: "/usr/bin/secret-tool"
    )
    monkeypatch.setattr(
        claude_profile,
        "_infisical_config",
        lambda: {
            "loggedInUsers": INFISICAL_USERS,
            "loggedInUserEmail": "alice@corp.example",
        },
    )
    monkeypatch.setattr(
        claude_profile, "_infisical_token", lambda email: tokens.get(email)
    )


def test_jwt_expired_future_is_valid() -> None:
    assert claude_profile._jwt_expired(_make_jwt(int(time.time()) + 10_000)) is False


def test_jwt_expired_past_is_expired() -> None:
    assert claude_profile._jwt_expired(_make_jwt(int(time.time()) - 10)) is True


def test_jwt_expired_malformed() -> None:
    assert claude_profile._jwt_expired("not-a-jwt") is True
    assert claude_profile._jwt_expired("a.b") is True


def test_jwt_expired_no_exp_claim() -> None:
    header = base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode()
    assert claude_profile._jwt_expired(f"{header}.{body}.s") is True


def test_infisical_token_returns_live_token(monkeypatch: pytest.MonkeyPatch) -> None:
    jwt = _make_jwt(int(time.time()) + 10_000)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=_keyring_blob(jwt))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._infisical_token("e@x") == jwt


def test_infisical_token_none_when_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    jwt = _make_jwt(int(time.time()) - 10)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=_keyring_blob(jwt))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._infisical_token("e@x") is None


def test_infisical_token_none_when_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._infisical_token("e@x") is None


def test_infisical_token_none_when_secret_tool_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_profile._infisical_token("e@x") is None


def test_infisical_login_matches() -> None:
    user = {
        "email": "joe@quickvm.com",
        "domain": "https://infisical.quickvm.example/api",
    }
    assert claude_profile._infisical_login_matches("joe@quickvm.com", user) is True
    assert claude_profile._infisical_login_matches("quickvm.com", user) is True
    assert claude_profile._infisical_login_matches("quickvm.example", user) is True
    assert claude_profile._infisical_login_matches("corp.example", user) is False


@pytest.mark.parametrize("entry", ["bob@corp.example", "bob", "orp.example"])
def test_infisical_login_matches_no_partial_names(entry: str) -> None:
    # Substring matching forwarded tokens for logins the user never named.
    jimbob = {
        "email": "jimbob@corp.example",
        "domain": "https://secrets.corp.example/api",
    }
    assert claude_profile._infisical_login_matches(entry, jimbob) is False


def test_infisical_logins_allowlist_and_active(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_infisical(
        monkeypatch,
        "corp.example,quickvm.com",
        {"alice@corp.example": "tok-corp", "joe@quickvm.com": "tok-qvm"},
    )
    by_email = {login.email: login for login in claude_profile._infisical_logins()}
    assert set(by_email) == {"alice@corp.example", "joe@quickvm.com"}
    assert by_email["alice@corp.example"].active is True
    assert by_email["joe@quickvm.com"].active is False
    assert by_email["alice@corp.example"].token == "tok-corp"


def test_infisical_logins_skips_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_infisical(
        monkeypatch,
        "corp.example,quickvm.com",
        {"alice@corp.example": "tok-corp", "joe@quickvm.com": None},
    )
    logins = claude_profile._infisical_logins()
    assert [login.email for login in logins] == ["alice@corp.example"]


def test_infisical_logins_no_match_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_infisical(monkeypatch, "nope.example", {})
    assert claude_profile._infisical_logins() == []


def test_infisical_logins_disabled_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_infisical", "")
    assert claude_profile._infisical_logins() == []


def test_infisical_logins_no_secret_tool_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_infisical", "corp.example")
    monkeypatch.setattr(claude_profile.shutil, "which", lambda _c: None)
    assert claude_profile._infisical_logins() == []


def test_with_infisical_env_disabled_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_infisical", "")
    assert claude_profile._with_infisical_env({"A": "1"}) == {"A": "1"}


def test_with_infisical_env_forwards_primary_and_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_infisical(
        monkeypatch,
        "quickvm.com,corp.example",
        {"joe@quickvm.com": "tok-qvm", "alice@corp.example": "tok-corp"},
    )
    env = claude_profile._with_infisical_env({"A": "1"})
    assert env["A"] == "1"
    # active login (corp.example) is primary even though quickvm is listed first
    assert env["INFISICAL_TOKEN"] == "tok-corp"
    assert env["INFISICAL_API_URL"] == "https://secrets.corp.example/api"
    assert env["INFISICAL_DOMAIN"] == "https://secrets.corp.example/api"
    profiles = json.loads(env["CLAUDE_SANDBOX_INFISICAL"])
    assert {p["email"] for p in profiles} == {"joe@quickvm.com", "alice@corp.example"}
    assert all({"email", "domain", "token"} <= set(p) for p in profiles)


def test_with_infisical_env_primary_falls_back_to_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # allowlist excludes the active login; primary falls back to the first match
    _setup_infisical(
        monkeypatch,
        "quickvm.com,home.example",
        {"joe@quickvm.com": "tok-qvm", "alice@home.example": "tok-home"},
    )
    env = claude_profile._with_infisical_env({})
    assert env["INFISICAL_TOKEN"] == "tok-qvm"
    assert env["INFISICAL_DOMAIN"] == "https://infisical.quickvm.example/api"


def test_with_infisical_env_all_expired_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_infisical(monkeypatch, "corp.example", {"alice@corp.example": None})
    assert claude_profile._with_infisical_env({"A": "1"}) == {"A": "1"}


def test_infisical_briefing_from_env() -> None:
    extra = {
        "INFISICAL_DOMAIN": "https://secrets.corp.example/api",
        "CLAUDE_SANDBOX_INFISICAL": json.dumps(
            [
                {
                    "email": "alice@corp.example",
                    "domain": "https://secrets.corp.example/api",
                    "token": "t",
                }
            ]
        ),
    }
    note = claude_profile._infisical_briefing(extra)
    assert "alice@corp.example" in note
    assert "--domain" in note


def test_infisical_briefing_empty_when_absent() -> None:
    assert claude_profile._infisical_briefing({"A": "1"}) == ""


def test_argv_includes_infisical_briefing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    extra = {
        "INFISICAL_DOMAIN": "https://secrets.corp.example/api",
        "CLAUDE_SANDBOX_INFISICAL": json.dumps(
            [
                {
                    "email": "alice@corp.example",
                    "domain": "https://secrets.corp.example/api",
                    "token": "t",
                }
            ]
        ),
    }
    argv = _build_sandbox_argv(profile, cwd, [], extra)
    briefing = argv[argv.index("--append-system-prompt") + 1]
    assert "infisical" in briefing.lower()
    assert "alice@corp.example" in briefing


# ---------------------------------------------------------------------------
# pulumi token forwarding
# ---------------------------------------------------------------------------


def _write_pulumi_creds(home: Path, creds: dict) -> None:
    pdir = home / ".pulumi"
    pdir.mkdir(exist_ok=True)
    (pdir / "credentials.json").write_text(json.dumps(creds))


def test_pulumi_token_reads_cloud_token(fake_home: Path) -> None:
    _write_pulumi_creds(
        fake_home,
        {
            "current": "https://api.pulumi.com",
            "accessTokens": {"https://api.pulumi.com": "pul-secret"},
        },
    )
    assert claude_profile._pulumi_token() == "pul-secret"


def test_pulumi_token_none_when_missing(fake_home: Path) -> None:
    assert claude_profile._pulumi_token() is None


def test_pulumi_token_none_for_self_managed_backend(fake_home: Path) -> None:
    _write_pulumi_creds(fake_home, {"current": "s3://my-bucket", "accessTokens": {}})
    assert claude_profile._pulumi_token() is None


def test_pulumi_token_none_when_no_token_for_current(fake_home: Path) -> None:
    _write_pulumi_creds(
        fake_home,
        {"current": "https://api.pulumi.com", "accessTokens": {"https://other": "x"}},
    )
    assert claude_profile._pulumi_token() is None


def test_with_pulumi_token_disabled_does_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def spy() -> str:
        nonlocal called
        called = True
        return "pul-x"

    monkeypatch.setattr(claude_profile, "_pulumi_token", spy)
    assert claude_profile._with_pulumi_token({"A": "1"}) == {"A": "1"}
    assert called is False


def test_with_pulumi_token_injects_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_pulumi", True)
    monkeypatch.setattr(claude_profile, "_pulumi_token", lambda: "pul-secret")
    assert claude_profile._with_pulumi_token({"A": "1"}) == {
        "A": "1",
        "PULUMI_ACCESS_TOKEN": "pul-secret",
    }


def test_with_pulumi_token_warns_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_pulumi", True)
    monkeypatch.setattr(claude_profile, "_pulumi_token", lambda: None)
    assert claude_profile._with_pulumi_token({"A": "1"}) == {"A": "1"}


# ---------------------------------------------------------------------------
# host env-var forwarding
# ---------------------------------------------------------------------------


def test_with_forwarded_env_disabled_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_profile.settings, "sandbox_forward_env", "")
    monkeypatch.setenv("SOME_TOKEN", "x")
    assert claude_profile._with_forwarded_env({"A": "1"}) == {"A": "1"}


def test_with_forwarded_env_forwards_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claude_profile.settings, "sandbox_forward_env", "FOO_TOKEN, BAR_TOKEN"
    )
    monkeypatch.setenv("FOO_TOKEN", "foo-val")
    monkeypatch.setenv("BAR_TOKEN", "bar-val")
    assert claude_profile._with_forwarded_env({"A": "1"}) == {
        "A": "1",
        "FOO_TOKEN": "foo-val",
        "BAR_TOKEN": "bar-val",
    }


def test_with_forwarded_env_silent_when_dotenv_supplies_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # set via `claude-profile env --set` (profile .env -> extra_env), absent from host env
    monkeypatch.setattr(
        claude_profile.settings, "sandbox_forward_env", "BUILDKITE_API_TOKEN"
    )
    monkeypatch.delenv("BUILDKITE_API_TOKEN", raising=False)
    out = claude_profile._with_forwarded_env({"BUILDKITE_API_TOKEN": "from-dotenv"})
    assert out["BUILDKITE_API_TOKEN"] == "from-dotenv"
    assert capsys.readouterr().err == ""


def test_with_forwarded_env_host_value_wins_over_dotenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        claude_profile.settings, "sandbox_forward_env", "BUILDKITE_API_TOKEN"
    )
    monkeypatch.setenv("BUILDKITE_API_TOKEN", "from-host")
    out = claude_profile._with_forwarded_env({"BUILDKITE_API_TOKEN": "from-dotenv"})
    assert out["BUILDKITE_API_TOKEN"] == "from-host"


def test_with_forwarded_env_skips_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claude_profile.settings, "sandbox_forward_env", "PRESENT_V,MISSING_V"
    )
    monkeypatch.setenv("PRESENT_V", "here")
    monkeypatch.delenv("MISSING_V", raising=False)
    assert claude_profile._with_forwarded_env({}) == {"PRESENT_V": "here"}


# ---------------------------------------------------------------------------
# MCP image cache
# ---------------------------------------------------------------------------


def test_image_ref_from_args_picks_registry_ref() -> None:
    f = claude_profile._image_ref_from_args
    assert (
        f(["run", "-i", "--rm", "-e", "TOKEN", "ghcr.io/org/img:latest", "stdio"])
        == "ghcr.io/org/img:latest"
    )
    # a -v path and an -e value must not be mistaken for the image
    assert (
        f(
            [
                "run",
                "--rm",
                "-v",
                "/home/u:/home/u:ro",
                "-e",
                "HOME=/tmp",
                "docker.io/mcp/x:1",
            ]
        )
        == "docker.io/mcp/x:1"
    )
    assert f(["run", "--rm", "alpine"]) is None  # no registry host segment
    assert f(["run", "--rm"]) is None


def test_mcp_container_images_discovers(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "command": "podman",
                        "args": ["run", "-i", "--rm", "-e", "GH", "ghcr.io/github/mcp"],
                    },
                    "windmill": {"type": "http", "url": "https://w.example/mcp"},
                    "local": {"command": "node", "args": ["/home/u/x.js"]},
                },
                "projects": {
                    "/some/proj": {
                        "mcpServers": {
                            "bk": {
                                "command": "podman",
                                "args": [
                                    "run",
                                    "--rm",
                                    "ghcr.io/buildkite/mcp:v1",
                                    "stdio",
                                ],
                            }
                        }
                    }
                },
            }
        )
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    assert claude_profile._mcp_container_images(profile, cwd) == [
        "ghcr.io/github/mcp",
        "ghcr.io/buildkite/mcp:v1",
    ]


def test_mcp_container_images_missing_config(tmp_path: Path) -> None:
    assert claude_profile._mcp_container_images(tmp_path, tmp_path) == []


def test_mcp_container_images_includes_ancestor_mcp_json(tmp_path: Path) -> None:
    """A container server declared in an ancestor .mcp.json runs in the VM, so its
    image belongs in the cache too."""
    profile = tmp_path / "prof"
    profile.mkdir()
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sonar": {
                        "command": "docker",
                        "args": ["run", "--rm", "ghcr.io/acme/sonar:2"],
                    },
                    "exa": {"command": "npx", "args": ["-y", "exa-mcp-server"]},
                }
            }
        )
    )
    cwd = tmp_path / "a" / "b"
    cwd.mkdir(parents=True)
    assert claude_profile._mcp_container_images(profile, cwd) == [
        "ghcr.io/acme/sonar:2"
    ]


def test_sandbox_cache_clear_deletes_only_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `podman system reset` also stops the user's rootless pause process and wipes the
    # run root that their other rootless containers share.
    store = tmp_path / "image-store"
    store.mkdir()
    monkeypatch.setattr(claude_profile, "_image_cache_dir", lambda: store)
    monkeypatch.setattr(claude_profile.shutil, "which", lambda name: None)
    ran: list[list[str]] = []
    monkeypatch.setattr(claude_profile, "_run_checked", ran.append)
    result = runner.invoke(app, ["sandbox-cache", "work", "--clear"])
    assert result.exit_code == 0, result.output
    assert ran == [
        [claude_profile.settings.podman_bin, "unshare", "rm", "-rf", str(store)]
    ]


def test_image_cache_dir_respects_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/x/data")
    assert claude_profile._image_cache_dir() == Path(
        "/x/data/claude-profile/image-store"
    )


def test_storage_cache_conf_written(tmp_path: Path) -> None:
    conf = claude_profile._storage_cache_conf(tmp_path)
    assert conf == claude_profile._sandbox_state_dir(tmp_path) / "storage.conf"
    text = conf.read_text()
    assert claude_profile.SANDBOX_IMAGE_STORE in text
    assert "additionalimagestores" in text
    assert "fuse-overlayfs" in text


def test_image_cache_mounts_when_populated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    (store / "overlay-images").mkdir(parents=True)
    monkeypatch.setattr(claude_profile, "_image_cache_dir", lambda: store)
    profile = tmp_path / "prof"
    profile.mkdir()
    mounts = claude_profile._image_cache_mounts(profile)
    assert f"{store}:{claude_profile.SANDBOX_IMAGE_STORE}:ro,z" in mounts
    assert any("storage.conf:/etc/containers/storage.conf:ro,z" in m for m in mounts)


def test_image_cache_mounts_empty_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_image_cache_dir", lambda: tmp_path / "nope")
    assert claude_profile._image_cache_mounts(tmp_path) == []


def test_all_commands_registered_in_known_commands() -> None:
    # main() dispatches any unknown first arg to _launch_profile as a profile name, so
    # every typer subcommand must be listed in KNOWN_COMMANDS or it gets shadowed.
    registered = {c.name for c in claude_profile.app.registered_commands if c.name}
    missing = registered - claude_profile.KNOWN_COMMANDS
    assert not missing, f"commands missing from KNOWN_COMMANDS: {missing}"


# ---------------------------------------------------------------------------
# sandbox briefing + sandbox-skill writer
# ---------------------------------------------------------------------------


def test_argv_includes_sandbox_briefing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, [], {})
    assert "--append-system-prompt" in argv
    briefing = argv[argv.index("--append-system-prompt") + 1]
    assert "sandbox" in briefing.lower()
    # The mounts are read-write: the agent must know its changes outlive the VM.
    assert "persists on the host" in briefing
    assert "git hooks" in briefing
    # Rootful nested containers leave subuid-owned files the host user can't delete.
    assert '--user "$(id -u):$(id -g)"' in briefing


@pytest.mark.parametrize("tty", [True, False])
def test_argv_allocates_a_tty_only_for_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tty: bool
) -> None:
    # With -t the in-VM claude's stdin is a TTY, so `git diff | claude-profile work -p`
    # silently dropped the piped diff.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)
    argv = _make_argv(monkeypatch, tmp_path, [])
    run_options = argv[: argv.index(claude_profile.settings.sandbox_image)]
    assert "-i" in run_options
    assert ("-t" in run_options) is tty


def test_argv_puts_sandbox_flags_before_the_users_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Appended after a subcommand's `--`, the sandbox's flags and briefing became the
    # MCP server's own arguments and were saved into the profile's .claude.json.
    monkeypatch.setattr(claude_profile.settings, "sandbox_chrome", True)
    user_args = ["mcp", "add", "fetch", "--", "uvx", "mcp-server-fetch"]
    argv = _make_argv(monkeypatch, tmp_path, user_args)
    claude_args = argv[argv.index(claude_profile.settings.sandbox_image) + 2 :]
    assert claude_args[-len(user_args) :] == user_args
    assert SKIP_PERMISSIONS_FLAG in claude_args[: -len(user_args)]
    assert "--chrome" in claude_args[: -len(user_args)]


@pytest.mark.parametrize(
    "mode", [["--permission-mode", "plan"], ["--permission-mode=plan"]]
)
def test_argv_keeps_an_explicit_permission_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: list[str]
) -> None:
    # claude ranks --dangerously-skip-permissions above --permission-mode, so adding it
    # would silently turn a requested plan-mode run into bypassPermissions.
    argv = _make_argv(monkeypatch, tmp_path, ["-p", *mode, "draft a plan"])
    assert SKIP_PERMISSIONS_FLAG not in argv


def test_argv_respects_user_system_prompt_given_with_equals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _make_argv(monkeypatch, tmp_path, ["--append-system-prompt=mine"])
    assert "--append-system-prompt" not in argv
    assert "--append-system-prompt=mine" in argv


def test_argv_respects_user_system_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, ["--append-system-prompt", "mine"], {})
    assert argv.count("--append-system-prompt") == 1
    assert argv[argv.index("--append-system-prompt") + 1] == "mine"


def test_render_sandbox_skill() -> None:
    content = claude_profile._render_sandbox_skill(["uv", "jq"])
    assert "{{INSTALLED_TOOLS}}" not in content
    assert "- `uv` —" in content
    assert "- `jq` —" in content


def test_sandbox_installed_tools_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="jq\nuv\nbogus\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._sandbox_installed_tools() == ["uv", "jq"]


def test_sandbox_skill_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(
        claude_profile, "_sandbox_installed_tools", lambda: ["uv", "jq"]
    )
    dest = tmp_path / "SKILL.md"
    result = runner.invoke(app, ["sandbox-skill", "--path", str(dest)])
    assert result.exit_code == 0
    text = dest.read_text()
    assert "- `uv` —" in text
    assert "- `jq` —" in text


def test_sandbox_skill_check_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_sandbox_installed_tools", lambda: ["uv"])
    dest = tmp_path / "SKILL.md"
    dest.write_text("stale")
    result = runner.invoke(app, ["sandbox-skill", "--check", "--path", str(dest)])
    assert result.exit_code == 1


def test_sandbox_skill_check_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_sandbox_installed_tools", lambda: ["uv"])
    dest = tmp_path / "SKILL.md"
    runner.invoke(app, ["sandbox-skill", "--path", str(dest)])
    result = runner.invoke(app, ["sandbox-skill", "--check", "--path", str(dest)])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# image-user guardrail (non-root image breaks the entrypoint's root block)
# ---------------------------------------------------------------------------


def test_sandbox_image_user_appuser(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="appuser\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _sandbox_image_user() == "appuser"


def test_sandbox_image_user_root_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _sandbox_image_user() == ""


def test_sandbox_image_user_no_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert _sandbox_image_user() == ""


def test_launch_warns_nonroot_image_but_proceeds(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_sandbox_image_user", lambda: "appuser")
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.setattr(
        claude_profile, "_build_forwarding", lambda: claude_profile._Forwarding([])
    )
    monkeypatch.chdir(tmp_path)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    assert mock_exec.called


def test_sandbox_mounts_ignore_links_planted_in_the_profile_dir(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The profile dir is writable from inside the VM. A file the launcher generated
    # there could be swapped for a symlink that the next launch writes through or
    # mounts: ~/.bashrc as known_hosts (read-write), ~/.ssh as the chrome dir.
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    (claude_profile._image_cache_dir() / "overlay-images").mkdir(parents=True)
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Bash(sudo *)"]}})
    )
    bashrc = fake_home / ".bashrc"
    bashrc.write_text("# untouched\n")
    keys = fake_home / ".ssh"
    keys.mkdir()
    victims = [fake_home / "victim-settings", fake_home / "victim-storage"]
    for victim in victims:
        victim.write_text("untouched")
    planted = {
        "known_hosts": bashrc,
        "chrome.sandbox": keys,
        "settings.sandbox.json": victims[0],
        "storage.sandbox.conf": victims[1],
    }
    for name, target in planted.items():
        (profile / name).symlink_to(target)
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    sources = {spec.split(":")[0] for spec in mounts if spec != "-v"}
    assert not sources & {str(profile / name) for name in planted}
    assert all(victim.read_text() == "untouched" for victim in victims)
    assert bashrc.read_text() == "# untouched\n"


def test_sandbox_mounts_pin_marker_and_env_read_only(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Deleting .sandbox from inside the VM would make the next plain launch a host
    # launch, which loads whatever the agent planted in .env.
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / SANDBOX_MARKER).touch()
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    config = claude_profile.SANDBOX_CONFIG_DIR
    assert f"{profile / SANDBOX_MARKER}:{config}/{SANDBOX_MARKER}:ro,z" in mounts
    assert f"{profile / '.env'}:{config}/.env:ro,z" in mounts
    # Created so there is something to pin, private like any .env.
    assert (profile / ".env").stat().st_mode & 0o777 == 0o600


def test_launch_refuses_a_symlinked_env_file(
    profiles_base: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A .env planted as a link to e.g. ~/.aws/credentials would have its KEY=VALUE
    # lines passed into the VM.
    secrets = fake_home / "credentials"
    secrets.write_text("aws_secret_access_key=s3cr3t\n")
    profile = profiles_base / "work"
    profile.mkdir(parents=True)
    (profile / ".env").symlink_to(secrets)
    with patch("os.execvpe") as mock_exec, pytest.raises(SystemExit) as exc_info:
        _launch_profile("work", [])
    assert exc_info.value.code == 1
    mock_exec.assert_not_called()


def test_sandbox_marker_planted_as_dangling_link_still_sandboxes(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / SANDBOX_MARKER).symlink_to(tmp_path / "gone")
    assert claude_profile._sandbox_enabled(profile) is True


def test_sandbox_mounts_known_hosts_global_ro_and_user_rw(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    ssh = fake_home / ".ssh"
    ssh.mkdir()
    (ssh / "known_hosts").write_text("git.example.org ssh-ed25519 AAAA\n")
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    # host file is the read-only global known_hosts (verification only)
    assert f"{ssh / 'known_hosts'}:/etc/ssh/ssh_known_hosts:ro,z" in mounts
    # per-profile writable user known_hosts persists newly accepted keys
    assert (
        f"{claude_profile._sandbox_state_dir(profile) / 'known_hosts'}:/home/appuser/.ssh/known_hosts:z"
        in mounts
    )
    assert (claude_profile._sandbox_state_dir(profile) / "known_hosts").exists()


def test_sandbox_mounts_user_known_hosts_without_host_file(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    # no host known_hosts -> no global mount, but the writable user file is still provided
    assert not any("/etc/ssh/ssh_known_hosts" in m for m in mounts)
    assert (
        f"{claude_profile._sandbox_state_dir(profile) / 'known_hosts'}:/home/appuser/.ssh/known_hosts:z"
        in mounts
    )


def test_sandbox_known_hosts_creates_empty(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    profile.mkdir()
    kh = claude_profile._sandbox_known_hosts(profile)
    assert kh == claude_profile._sandbox_state_dir(profile) / "known_hosts"
    assert kh.exists() and kh.read_text() == ""


def test_sandbox_known_hosts_preserves_existing(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    profile.mkdir()
    (claude_profile._sandbox_state_dir(profile) / "known_hosts").write_text(
        "host1 ssh-ed25519 KEY\n"
    )
    assert (
        claude_profile._sandbox_known_hosts(profile).read_text()
        == "host1 ssh-ed25519 KEY\n"
    )


# ---------------------------------------------------------------------------
# shared settings
# ---------------------------------------------------------------------------

HOOK_URL = "http://127.0.0.1:8080/hook?profile={profile}"
SHARED: dict[str, Any] = {
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {"type": "http", "url": HOOK_URL, "timeout": 5},
                    {"type": "command", "command": "echo {profile}"},
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "http", "url": "https://hooks.example.com/check"}],
            }
        ],
    }
}


def _write_shared(profiles_base: Path, data: object) -> None:
    profiles_base.mkdir(parents=True, exist_ok=True)
    (profiles_base / claude_profile.SHARED_SETTINGS).write_text(json.dumps(data))


def _settings_arg(argv: list[str]) -> dict[str, Any]:
    return json.loads(argv[argv.index("--settings") + 1])


def test_launch_passes_shared_settings_with_the_profile_name(
    profiles_base: Path,
) -> None:
    (profiles_base / "work").mkdir(parents=True)
    _write_shared(profiles_base, SHARED)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", ["--resume"])
    _bin, argv, _env = mock_exec.call_args[0]
    stop = _settings_arg(argv)["hooks"]["Stop"][0]["hooks"]
    assert stop[0]["url"] == "http://127.0.0.1:8080/hook?profile=work"
    assert stop[1]["command"] == "echo work"
    assert argv[-1] == "--resume"


def test_launch_without_shared_settings_passes_none(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", [])
    assert "--settings" not in mock_exec.call_args[0][1]


def test_launch_leaves_an_explicit_settings_flag_alone(profiles_base: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    _write_shared(profiles_base, SHARED)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("work", ["--settings", "mine.json"])
    argv = mock_exec.call_args[0][1]
    assert argv.count("--settings") == 1
    assert argv[argv.index("--settings") + 1] == "mine.json"


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_launch_exits_on_a_broken_shared_settings_file(
    profiles_base: Path, content: str
) -> None:
    (profiles_base / "work").mkdir(parents=True)
    (profiles_base / claude_profile.SHARED_SETTINGS).write_text(content)
    with pytest.raises(SystemExit) as exc_info, patch("os.execvpe"):
        _launch_profile("work", [])
    assert exc_info.value.code == 1


def test_sandbox_repoints_loopback_hooks_at_the_host(profiles_base: Path) -> None:
    _write_shared(profiles_base, SHARED)
    args, host_loopback = claude_profile._shared_settings_args(
        "personal", [], sandbox=True
    )
    hooks = json.loads(args[1])["hooks"]
    loopback = claude_profile.SANDBOX_HOST_LOOPBACK
    assert hooks["Stop"][0]["hooks"][0]["url"] == (
        f"http://{loopback}:8080/hook?profile=personal"
    )
    assert (
        hooks["PreToolUse"][0]["hooks"][0]["url"] == "https://hooks.example.com/check"
    )
    assert host_loopback is True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://user:pw@127.0.0.1:3000/x", "http://user:pw@{loopback}:3000/x"),
        ("http://LOCALHOST/x", "http://{loopback}/x"),
        # A malformed port crashed every sandbox launch; claude reports it instead.
        ("http://localhost:abc/x", "http://{loopback}:abc/x"),
    ],
)
def test_sandbox_loopback_rewrite_changes_only_the_host(
    url: str, expected: str
) -> None:
    data = {"hooks": {"Stop": [{"hooks": [{"type": "http", "url": url}]}]}}
    assert claude_profile._rewrite_loopback_hooks(data) is True
    loopback = claude_profile.SANDBOX_HOST_LOOPBACK
    assert data["hooks"]["Stop"][0]["hooks"][0]["url"] == expected.format(
        loopback=loopback
    )


def test_host_launch_keeps_loopback_hooks(profiles_base: Path) -> None:
    _write_shared(profiles_base, SHARED)
    args, host_loopback = claude_profile._shared_settings_args(
        "work", [], sandbox=False
    )
    url = json.loads(args[1])["hooks"]["Stop"][0]["hooks"][0]["url"]
    assert url == "http://127.0.0.1:8080/hook?profile=work"
    assert host_loopback is False


def test_sandbox_needs_no_loopback_mapping_without_loopback_hooks(
    profiles_base: Path,
) -> None:
    _write_shared(
        profiles_base, {"hooks": {"PreToolUse": SHARED["hooks"]["PreToolUse"]}}
    )
    _args, host_loopback = claude_profile._shared_settings_args(
        "work", [], sandbox=True
    )
    assert host_loopback is False


def test_argv_maps_host_loopback_for_shared_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    argv = _build_sandbox_argv(profile, cwd, [], {}, host_loopback=True)
    loopback = claude_profile.SANDBOX_HOST_LOOPBACK
    assert f"--network=pasta:--map-host-loopback,{loopback}" in argv


def test_sandbox_launch_with_loopback_hooks_maps_host_loopback(
    profiles_base: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profiles_base / "personal"
    profile.mkdir(parents=True)
    (profile / SANDBOX_MARKER).touch()
    _write_shared(profiles_base, SHARED)
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    monkeypatch.setattr(
        claude_profile, "_build_forwarding", lambda: claude_profile._Forwarding([])
    )
    monkeypatch.chdir(tmp_path)
    with patch("os.execvpe") as mock_exec:
        _launch_profile("personal", [])
    _bin, argv, _env = mock_exec.call_args[0]
    assert any(arg.startswith("--network=pasta:--map-host-loopback") for arg in argv)
    url = _settings_arg(argv)["hooks"]["Stop"][0]["hooks"][0]["url"]
    assert url.startswith(f"http://{claude_profile.SANDBOX_HOST_LOOPBACK}:8080/")
