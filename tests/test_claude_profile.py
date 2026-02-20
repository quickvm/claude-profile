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
    result = runner.invoke(app, ["add", "work"])
    assert result.exit_code == 0
    assert (profiles_base / "work").is_dir()


def test_add_copies_files_from_claude_dir(profiles_base: Path, fake_home: Path) -> None:
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text('{"theme": "dark"}')
    (claude_dir / "statusline.sh").write_text("#!/bin/sh\necho ok")
    (claude_dir / "CLAUDE.md").write_text("# instructions")

    runner.invoke(app, ["add", "work"])

    profile = profiles_base / "work"
    assert (profile / "settings.json").read_text() == '{"theme": "dark"}'
    assert (profile / "statusline.sh").read_text() == "#!/bin/sh\necho ok"
    assert (profile / "CLAUDE.md").read_text() == "# instructions"


def test_add_skips_missing_source_files(profiles_base: Path, fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    result = runner.invoke(app, ["add", "work"])
    assert result.exit_code == 0
    profile = profiles_base / "work"
    assert not (profile / "settings.json").exists()
    assert not (profile / "statusline.sh").exists()
    assert not (profile / "CLAUDE.md").exists()


def test_add_fails_if_profile_exists(profiles_base: Path, fake_home: Path) -> None:
    (profiles_base / "work").mkdir(parents=True)
    result = runner.invoke(app, ["add", "work"])
    assert result.exit_code == 1


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
