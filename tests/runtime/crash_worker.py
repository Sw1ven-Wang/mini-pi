"""Subprocess-only hard crash fixture. No production HTTP failpoint endpoint."""

import asyncio
import json
import os
from pathlib import Path
import sys

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.workspace_ir import WorkspaceChangeSet
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.runtime.kernel import RuntimeKernel


class HardCrash:
    def __init__(self, point):
        self.point = point

    def maybe_fail(self, point):
        if point == self.point:
            os._exit(86)


def main():
    workspace = Workspace(Path(sys.argv[1]))
    kernel = RuntimeKernel(workspace, state_root=Path(sys.argv[2]))
    execution = kernel.start("crash-session", command="trusted test")
    change_set = WorkspaceChangeSet.model_validate(json.loads(sys.argv[4]))
    execution.prepare_proposal(change_set)
    mutation = WorkspaceMutationRuntime(workspace, FileMutationQueue(), CheckpointStore(),
        journal=execution.journal(), fault_injector=HardCrash(sys.argv[3]))
    asyncio.run(mutation.apply(change_set))
    raise AssertionError("expected hard crash was not reached")


if __name__ == "__main__":
    main()
