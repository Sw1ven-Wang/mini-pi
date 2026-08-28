from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class ShellType(str, Enum):
    BASH = "bash"
    POWERSHELL = "powershell"


@dataclass(frozen=True)
class ShellConfig:
    shell_type: ShellType
    executable: str
    args: tuple[str, ...]


def default_shell_config() -> ShellConfig:
    if os.name == "nt":
        return powershell_config()

    return bash_config()
def powershell_config() -> ShellConfig:
    return ShellConfig(
        shell_type=ShellType.POWERSHELL,
        executable="powershell",
        args=(
            "-NoProfile",
            "-NonInteractive",
            "-Command",
        ),
    )
def bash_config() -> ShellConfig:
    return ShellConfig(
        shell_type=ShellType.BASH,
        executable="bash",
        args=(
            "-lc",
        ),
    )