"""Shared Web/SDK assembly. Transports supply providers, approvals and observers."""

from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.agents.editor import Editor
from coding_agent.agents.planner import Planner
from coding_agent.agents.verifier import Verifier
from coding_agent.agents.viewer import Viewer
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.context_budget import ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.orchestration import CodingOrchestrator
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import default_shell_config
from coding_agent.repository_map import RepositoryMapBuilder
from coding_agent.runtime.acceptance import default_contract
from coding_agent.runtime.models import RecoveryRequiredError
from coding_agent.runtime.approvals import DurableApprovalHandler
from coding_agent.safety.factory import permission_engine_from_workspace
from coding_agent.safety.settings import PermissionMode
from coding_agent.sandbox.factory import sandbox_from_settings
from coding_agent.session import CodingSession
from coding_agent.verification.pipeline import VerificationPipeline
from coding_agent.verification.presets import pytest_verifier


def build_coding_runtime(*, execution, provider, model, approval_handler,
                         verify_command, event_bus, trace_context, preview_observer=None,
                         resume=False, goal=None):
    workspace = execution.workspace
    engine = permission_engine_from_workspace(workspace.root, provider=provider, model=model)
    if not getattr(approval_handler, "durable", None):
        approval_handler = DurableApprovalHandler(execution, approval_handler)
    dispatcher = ToolDispatcher(ToolRegistry([]), engine, approval_handler=approval_handler)
    kwargs = dict(provider=provider, model=model, tool_dispatcher=dispatcher,
        session_store=execution.kernel.session_store,
        context_manager=ContextManager(budget=ContextBudget(max_tokens=64_000, reserve_tokens=8_192)),
        event_bus=event_bus, trace_context=trace_context)
    if resume:
        session = CodingSession.resume(execution.run.session_id, **kwargs)
        # Current host policy wins; preserve AUTO's persisted safety downgrade.
        if not (engine.settings.permission_mode == PermissionMode.AUTO
                and session.state.permission_mode == PermissionMode.DEFAULT
                and session.state.auto_total_blocks):
            engine.mode = engine.settings.permission_mode
        session.persist_runtime_state()
    else:
        session = CodingSession.create(user_goal=goal, session_id=execution.run.session_id, **kwargs)
    engine.update_user_request(session.state.user_goal)
    session.state.active_run_id = execution.run_id
    session.persist_runtime_state()
    execution.permission_engine = engine
    execution.restore_grants(session.session_id)
    configured = default_contract(verify_command).checks[0]
    registered = next((check for check in execution.acceptance.contract().checks
                       if check.check_id == configured.check_id), None)
    if registered is None or registered.definition_digest != configured.definition_digest:
        raise RecoveryRequiredError("host verification configuration changed")
    mutation = WorkspaceMutationRuntime(workspace, FileMutationQueue(), CheckpointStore())
    runner = ProcessRunner(parent_lifetime_guard=True, resource_recorder=execution.record_process_resources)
    execution.process_tracking_enabled = True
    orchestrator = CodingOrchestrator(workspace=workspace,
        planner=Planner(provider, model), viewer=Viewer(workspace, observation_store=session.observations),
        editor=Editor(provider, model), mutation_runtime=mutation,
        verifier=Verifier(VerificationPipeline([pytest_verifier(runner, default_shell_config(),
            command=verify_command, sandbox=sandbox_from_settings(engine.settings.sandbox,
                process_runner=runner,
                resource_recorder=execution.record_sandbox_resources,
                event_bus=event_bus, trace_context=trace_context, session_id=session.session_id))])),
        repository_map_builder=RepositoryMapBuilder(workspace), event_bus=event_bus,
        trace_context=trace_context, mutation_preview_observer=preview_observer,
        runtime_execution=execution)
    return session, orchestrator
