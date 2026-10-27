"""Tests for ShellConfig factories."""

import os
from dataclasses import FrozenInstanceError

import pytest

from coding_agent.process.shell import (
    ShellType,
    bash_config,
    default_shell_config,
    powershell_config,
)


def test_powershell_config():
    config = powershell_config()

    assert config.shell_type == ShellType.POWERSHELL
    assert config.executable == "powershell"
    assert config.args == (
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    )


def test_bash_config():
    config = bash_config()

    assert config.shell_type == ShellType.BASH
    assert config.executable == "bash"
    assert config.args == ("--noprofile", "--norc", "-c")


def test_default_shell_config_windows(
    monkeypatch,
):
    monkeypatch.setattr(os, "name", "nt")

    config = default_shell_config()

    assert config.shell_type == ShellType.POWERSHELL


def test_default_shell_config_posix(
    monkeypatch,
):
    monkeypatch.setattr(os, "name", "posix")

    config = default_shell_config()

    assert config.shell_type == ShellType.BASH


def test_shell_config_is_immutable():
    config = powershell_config()

    with pytest.raises(FrozenInstanceError):
        config.executable = "pwsh"  # type: ignore[misc]
