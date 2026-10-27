"""Crash after compensation bytes are restored, before its journal commit."""
import os
from pathlib import Path
import sys

from coding_agent.filesystem.workspace import Workspace
from coding_agent.runtime.journal import MutationJournal
from coding_agent.runtime.kernel import RuntimeKernel

kernel = RuntimeKernel(Workspace(Path(sys.argv[1])), state_root=Path(sys.argv[2]))
kernel.mark_interrupted()
original = MutationJournal._restore


def crash(self, row):
    original(self, row)
    os._exit(87)


MutationJournal._restore = crash
kernel.resume(kernel.store.runs()[0].run_id)
raise AssertionError("compensation crash point was not reached")
