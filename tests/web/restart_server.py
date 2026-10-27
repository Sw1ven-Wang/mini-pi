"""Real HTTP restart fixture; only model responses and crash points are fake.

Not imported by production. State and workspace survive replacement processes.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import sys

import uvicorn

from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.runtime.acceptance import default_contract, Requirement
from coding_agent.web.app import create_app
from coding_agent.web.config import parse_allowed_root
from tests.web.live_server import FakeRegistry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--crash-after-effect", action="store_true")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--replay-safe", action="store_true")
    parser.add_argument("--crash-after-verification-process", action="store_true")
    parser.add_argument("--crash-during-verification", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    workspace = root / "projects" / "demo"
    if not workspace.exists():
        (workspace / "tests").mkdir(parents=True)
        (workspace / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (workspace / "tests/test_app.py").write_text("from app import value\ndef test_value():\n    assert value() == 2\n", encoding="utf-8")
    if args.crash_after_effect:
        original = WorkspaceMutationRuntime._apply_one

        async def crash(self, index, operation):
            await original(self, index, operation)
            os._exit(86)  # File effect exists, APPLIED commit has not happened.

        WorkspaceMutationRuntime._apply_one = crash
    if args.crash_after_verification_process:
        from coding_agent.process.runner import ProcessRunner
        original_run = ProcessRunner.run
        async def crash_after_process(self, *values, **kwargs):
            result = await original_run(self, *values, **kwargs)
            if self.resource_recorder:
                os._exit(88)  # Lifecycle persisted, acceptance evidence not committed.
            return result
        ProcessRunner.run = crash_after_process
    if args.crash_during_verification:
        from coding_agent.runtime.kernel import RuntimeExecution
        original_record = RuntimeExecution.record_process_resources
        def crash_owned(self, value):
            original_record(self, value)
            if value["state"] == "owned":
                os._exit(89)  # Owned Windows Job survives until child tree stops.
        RuntimeExecution.record_process_resources = crash_owned
    os.environ["MINI_PI_E2E_PROVIDER_KEY"] = "sk-test-only-server-sentinel"
    executable = ("& '" + sys.executable.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(sys.executable))
    command = executable + " -m pytest -q -p no:cacheprovider"
    contract = default_contract(command)
    contract.checks[0].replay_safe = args.replay_safe
    from coding_agent.process.runner import ProcessRunner
    from coding_agent.errors import ProcessStartFailedError
    diagnostic_run = ProcessRunner.run
    async def diagnose_start(self, *values, **kwargs):
        try:
            return await diagnostic_run(self, *values, **kwargs)
        except ProcessStartFailedError as exc:
            # Test-only process lifecycle diagnostics, no command/env or keys.
            print("TEST_PROCESS_START_FAILURE", repr(exc.__cause__), file=sys.stderr, flush=True)
            raise
    ProcessRunner.run = diagnose_start
    if args.manual:
        contract.coverage = "registered_checks_and_manual_review"
        contract.requirements.append(Requirement(requirement_id="human_review", description="确认任务行为符合要求", manual_confirmation=True))
    app = create_app(roots=[parse_allowed_root(f"Projects={root / 'projects'}")],
        providers=FakeRegistry(), verify_command=command, acceptance_contract=contract,
        state_root=root / "private", static_dir=Path(__file__).resolve().parents[2] / "web/dist")
    uvicorn.run(app, host="127.0.0.1", port=8877, access_log=False)


if __name__ == "__main__":
    main()
