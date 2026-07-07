"""Tests for claude_profile."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

import claude_profile
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
def _reset_sandbox_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the sandbox env overrides off unless a test sets them."""
    monkeypatch.setattr(claude_profile.settings, "sandbox", None)
    monkeypatch.setattr(claude_profile.settings, "sandbox_ssh_agent", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_gpg_agent", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_clipboard", False)
    monkeypatch.setattr(claude_profile.settings, "sandbox_gh", False)
    # Default the image-user check to root so launch tests skip the real podman call.
    monkeypatch.setattr(claude_profile, "_sandbox_image_user", lambda: "")


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
    result = runner.invoke(app, ["add", "work"], input="y\ny\n")
    assert result.exit_code == 0
    assert (profiles_base / "work").is_dir()


def test_add_copies_files_from_claude_dir(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text('{"theme": "dark"}')
    (claude_dir / "statusline.sh").write_text("#!/bin/sh\necho ok")
    (claude_dir / "CLAUDE.md").write_text("# instructions")

    # Answer n to both link prompts (no global dirs exist)
    runner.invoke(app, ["add", "work"], input="n\nn\n")

    profile = profiles_base / "work"
    assert (profile / "settings.json").read_text() == '{"theme": "dark"}'
    assert (profile / "statusline.sh").read_text() == "#!/bin/sh\necho ok"
    assert (profile / "CLAUDE.md").read_text() == "# instructions"


def test_add_skips_missing_source_files(profiles_base: Path, fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    result = runner.invoke(app, ["add", "work"], input="n\nn\n")
    assert result.exit_code == 0
    profile = profiles_base / "work"
    assert not (profile / "settings.json").exists()
    assert not (profile / "statusline.sh").exists()
    assert not (profile / "CLAUDE.md").exists()


def test_add_fails_if_profile_exists(profiles_base: Path, fake_home: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["add", "work"])
    assert result.exit_code == 1


def test_add_default_yes_creates_symlinks(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "commands").mkdir()
    (claude_dir / "skills").mkdir()

    result = runner.invoke(app, ["add", "work"], input="\n\n")
    assert result.exit_code == 0
    profile = profiles_base / "work"
    assert (profile / "commands").is_symlink()
    assert (profile / "skills").is_symlink()


def test_add_decline_both_creates_isolated_dirs(
    profiles_base: Path, fake_home: Path
) -> None:
    (fake_home / ".claude").mkdir()
    result = runner.invoke(app, ["add", "work"], input="n\nn\n")
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
    result = runner.invoke(app, ["add", "work"], input="y\nn\n")
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
    result = runner.invoke(app, ["add", "work"], input="y\ny\n")
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
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    mounts = _sandbox_mounts(tmp_path / "prof", cwd)
    assert mounts.count("-v") == 2
    assert f"{cwd}:{cwd}:z" in mounts


def test_sandbox_mounts_includes_external_git_dir(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "wt" / "feature"
    cwd.mkdir(parents=True)
    git_dir = tmp_path / "main" / ".git"
    git_dir.mkdir(parents=True)
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: git_dir)
    mounts = _sandbox_mounts(tmp_path / "prof", cwd)
    assert mounts.count("-v") == 3
    assert f"{git_dir}:{git_dir}:z" in mounts


def test_sandbox_mounts_skips_git_dir_inside_cwd(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: cwd / ".git")
    mounts = _sandbox_mounts(tmp_path / "prof", cwd)
    assert mounts.count("-v") == 2


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
    assert overlay == prof / "settings.sandbox.json"
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
    overlay = prof / "settings.sandbox.json"
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


def test_argv_passes_env_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    argv = _make_argv(monkeypatch, tmp_path, [], {"ANTHROPIC_API_KEY": "sk-test"})
    assert "ANTHROPIC_API_KEY=sk-test" in argv


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
    result = runner.invoke(app, ["add", "work", "--sandbox"], input="n\nn\n")
    assert result.exit_code == 0
    assert (profiles_base / "work" / SANDBOX_MARKER).exists()


def test_add_without_sandbox_no_marker(profiles_base: Path, fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    result = runner.invoke(app, ["add", "work"], input="n\nn\n")
    assert result.exit_code == 0
    assert not (profiles_base / "work" / SANDBOX_MARKER).exists()


def test_add_sandbox_hints_build_when_image_absent(
    profiles_base: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_home / ".claude").mkdir()
    monkeypatch.setattr(claude_profile, "_sandbox_image_exists", lambda: False)
    result = runner.invoke(app, ["add", "work", "--sandbox"], input="n\nn\n")
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


def test_launch_sandbox_loads_env_into_argv(
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
    _binname, argv, _env = mock_exec.call_args[0]
    assert "ANTHROPIC_API_KEY=sk-xyz" in argv


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
# linked commands/skills read-only mounts
# ---------------------------------------------------------------------------


def test_linked_dir_mount_for_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    global_skills = tmp_path / "global_skills"
    global_skills.mkdir()
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
    assert all("ro,z" not in m for m in mounts)


def test_linked_dir_broken_symlink_skipped(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / "commands").symlink_to(tmp_path / "missing")
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert all("ro,z" not in m for m in mounts)


def test_linked_dir_symlink_chain_mounts_real_at_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    intermediate = tmp_path / "intermediate"
    intermediate.symlink_to(canonical)
    (profile / "skills").symlink_to(intermediate)
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    # real content mounted at the link's immediate (absolute) target
    assert f"{canonical}:{intermediate}:ro,z" in mounts


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
    fwd = claude_profile._Forwarding([(host, guest, 2222)], gpg_pubkeys_b64="QUJD")
    env = claude_profile._forwarding_env(fwd)
    assert f"CLAUDE_SANDBOX_FORWARDS={guest}=2222" in env
    assert "GNUPGHOME=/home/appuser/.gnupg" in env
    assert "CLAUDE_SANDBOX_GPG_PUBKEYS=QUJD" in env


def test_gpg_extra_socket_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="/nope/S.gpg-agent.extra\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._gpg_extra_socket() is None


def test_gpg_extra_socket_no_gpgconf(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert claude_profile._gpg_extra_socket() is None


def test_export_gpg_pubkeys_b64(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, stdout=b"PUBKEYBYTES")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert claude_profile._export_gpg_pubkeys() == base64.b64encode(
        b"PUBKEYBYTES"
    ).decode("ascii")


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
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: 5000)
    fwd = claude_profile._build_forwarding()
    assert fwd.forwards == [(auth, auth, 5000)]
    assert fwd.ssh_auth_sock == auth
    assert fwd.gpg_pubkeys_b64 is None


def test_build_forwarding_gpg_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    extra = tmp_path / "S.gpg-agent.extra"
    extra.touch()
    monkeypatch.setattr(claude_profile.settings, "sandbox_gpg_agent", True)
    monkeypatch.setattr(claude_profile, "_gpg_extra_socket", lambda: extra)
    monkeypatch.setattr(claude_profile, "_export_gpg_pubkeys", lambda: "QUJD")
    monkeypatch.setattr(claude_profile, "_free_tcp_port", lambda: 6000)
    fwd = claude_profile._build_forwarding()
    guest = Path(claude_profile.SANDBOX_GNUPGHOME) / "S.gpg-agent"
    assert fwd.forwards == [(extra, guest, 6000)]
    assert fwd.ssh_auth_sock is None
    assert fwd.gpg_pubkeys_b64 == "QUJD"


def test_build_forwarding_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fwd = claude_profile._build_forwarding()
    assert fwd.forwards == []
    assert fwd.ssh_auth_sock is None
    assert fwd.gpg_pubkeys_b64 is None


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
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
        patch("os.execvpe") as mock_exec,
        pytest.raises(SystemExit) as exc_info,
    ):
        _launch_profile("work", [])
    assert exc_info.value.code == 0
    mock_exec.assert_not_called()
    run.assert_called_once()
    assert any(arg.startswith("--network=pasta") for arg in run.call_args[0][0])
    assert started == [(sock, 1234)]


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
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
        patch("os.execvpe") as mock_exec,
        pytest.raises(SystemExit) as exc_info,
    ):
        _launch_profile("work", [])
    assert exc_info.value.code == 0
    mock_exec.assert_not_called()
    run.assert_called_once()
    assert any(arg.startswith("--network=pasta") for arg in run.call_args[0][0])
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


def test_sandbox_mounts_includes_known_hosts(
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
    assert f"{ssh / 'known_hosts'}:/home/appuser/.ssh/known_hosts:ro,z" in mounts


def test_sandbox_mounts_no_known_hosts(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_profile, "_git_common_dir", lambda c: None)
    profile = tmp_path / "prof"
    profile.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    mounts = _sandbox_mounts(profile, cwd)
    assert not any("known_hosts" in m for m in mounts)
