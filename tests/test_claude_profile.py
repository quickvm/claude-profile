"""Tests for claude_profile."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import claude_profile
from claude_profile import _launch_profile, app, main

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
