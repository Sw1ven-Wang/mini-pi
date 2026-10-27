from pathlib import Path

import pytest

from coding_agent.filesystem.workspace import Workspace


@pytest.mark.parametrize("path", [
    "NUL", "con.txt", "folder/AUX.py", "COM1", "LPT9.log", r"\\?\C:\temp\x"
])
def test_windows_device_paths_are_rejected(tmp_path: Path, path: str):
    workspace = Workspace(tmp_path)
    with pytest.raises(ValueError):
        workspace.resolve(path)
