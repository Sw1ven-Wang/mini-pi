import asyncio
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from coding_agent.process.runner import ProcessRunner


def test_owned_process_tree_stops(tmp_path):
    if os.name == "nt":
        result = subprocess.run([sys.executable, "-m", "tests.runtime.owner_crash_worker", str(tmp_path)],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 86, result.stdout + result.stderr
    else:
        script = Path(__file__).with_name("process_heartbeat.py").resolve()
        command = " ".join(shlex.quote(str(value)) for value in (sys.executable, script, tmp_path))
        result = asyncio.run(ProcessRunner(parent_lifetime_guard=True).run(command, cwd=tmp_path,
                                                                           timeout_seconds=0.5))
        assert result.timed_out
    time.sleep(0.15)
    heartbeat = (tmp_path / "heartbeat").read_text()
    time.sleep(0.25)
    assert (tmp_path / "heartbeat").read_text() == heartbeat
