"""V6 session data models.

Runtime dependencies (Provider, Workspace, locks and clients) are deliberately
absent from these models.  ``CodingSession`` orchestration is defined below the
data layer later in this module.
"""

from __future__ import annotations

import re
import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from ai.context import Context
from ai.messages import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from ai.models import Model
from ai.providers.base import Provider
from agent.agent_loop import run_agent_loop
from agent.tool_dispatcher import ToolDispatcher
from agent.tools import AgentToolResult
from coding_agent.observations import ObservedFileState
from coding_agent.observations import (
    CodingObservationObserver,
    ObservationStore,
    VersionResolver,
)
from coding_agent.errors import SessionReconciliationRequiredError
from coding_agent.agents.models import TaskPlan
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.events import AgentEventType
from coding_agent.observability.tracing import TraceContext
from coding_agent.safety.auto_classifier import SafetyConstraints
from coding_agent.safety.settings import PermissionMode

if TYPE_CHECKING:
    from coding_agent.context_manager import ContextManager
    from coding_agent.session_store import SessionStore


_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "secret",
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{4,}",
        re.IGNORECASE,
    ),
)


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionState(BaseModel):
    session_id: str
    created_at: datetime = Field(default_factory=lambda: _now())
    updated_at: datetime = Field(default_factory=lambda: _now())
    status: SessionStatus = SessionStatus.ACTIVE
    user_goal: str
    turn_count: int = Field(default=0, ge=0)
    compacted_summary: str | None = None
    observations: list[ObservedFileState] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    interrupted: bool = False
    compaction_count: int = Field(default=0, ge=0)
    current_plan: TaskPlan | None = None
    plan_revision: int = Field(default=0, ge=0)
    latest_replan_reason: str | None = None
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    safety_constraints: SafetyConstraints = Field(default_factory=SafetyConstraints)
    auto_consecutive_blocks: int = Field(default=0, ge=0)
    auto_total_blocks: int = Field(default=0, ge=0)
    active_run_id: str | None = None

    @field_validator("session_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_session_id(value)

    @field_validator("metadata", mode="before")
    @classmethod
    def _sanitize_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}

        sanitized = _sanitize_json_value(value)

        if not isinstance(sanitized, dict):
            raise ValueError("session metadata 必须是 JSON object")

        return sanitized

    @model_validator(mode="after")
    def _validate_plan_state(self) -> "SessionState":
        if self.current_plan is None and self.plan_revision != 0:
            raise ValueError("current_plan 为空时 plan_revision 必须为 0")
        if (
            self.current_plan is not None
            and self.plan_revision != self.current_plan.revision
        ):
            raise ValueError("plan_revision 必须等于 current_plan.revision")
        return self


class SessionSnapshot(BaseModel):
    # Missing means V6-V10. New sessions explicitly write V11 format 2.
    format_version: int = 1
    state: SessionState
    context: Context

    @field_validator("format_version")
    @classmethod
    def supported_format(cls, value: int) -> int:
        if value not in (1, 2):
            raise ValueError("unsupported SessionSnapshot format_version")
        return value


def validate_session_id(session_id: str) -> str:
    """拒绝绝对路径、遍历和其它不安全 session id。"""

    if (
        not isinstance(session_id, str)
        or not _SAFE_SESSION_ID.fullmatch(session_id)
        or session_id in {".", ".."}
    ):
        raise ValueError("session_id 格式不安全")

    return session_id


def _sanitize_json_value(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()

    if any(secret in lowered for secret in _SECRET_KEYS):
        return _Redacted

    if isinstance(value, str):
        return _redact_secret_text(value)

    if value is None or isinstance(value, (int, float, bool)):
        return value

    if isinstance(value, dict):
        result: dict[str, Any] = {}

        for item_key, item_value in value.items():
            if not isinstance(item_key, str):
                raise ValueError("session metadata key 必须是字符串")

            sanitized = _sanitize_json_value(
                item_value,
                key=item_key,
            )

            if sanitized is not _Redacted:
                result[item_key] = sanitized

        return result

    if isinstance(value, (list, tuple)):
        result_list = []

        for item in value:
            sanitized = _sanitize_json_value(item)

            if sanitized is not _Redacted:
                result_list.append(sanitized)

        return result_list

    raise ValueError(
        "session metadata 只能包含 JSON 数据，不能包含 runtime object"
    )


def sanitize_session_payload(value: Any) -> dict[str, Any]:
    """Recursively remove secret fields and redact common inline tokens."""

    sanitized = _sanitize_json_value(value)

    if not isinstance(sanitized, dict):
        raise ValueError("session payload 必须是 JSON object")

    return sanitized


def _redact_secret_text(value: str) -> str:
    redacted = value

    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)

    return redacted


class _RedactedType:
    pass


_Redacted = _RedactedType()


def _now() -> datetime:
    return datetime.now(timezone.utc)


V6_RUNTIME_PROMPT = """文件版本由 Host 管理。

修改、移动或删除已有文件前仍应先 read，不要手工猜测或复制文件 hash。
如果工具返回 stale/version conflict，必须重新 read 当前文件，再决定新的修改。
Session summary 可能代表压缩后的历史，但磁盘当前状态始终优先于 summary 和历史观察。"""

INTERRUPTED_SESSION_PROMPT = """<interrupted_session>
上次进程在 tool call/result 完整配对前中断。工具可能已经产生副作用。
不要重放未完成的 edit/delete/move/patch；先 read/search/verify 当前磁盘状态，再重新规划。
</interrupted_session>"""


class SessionPersistenceObserver:
    """Persist every context update and compact only complete tool groups."""

    def __init__(
        self,
        *,
        state: SessionState,
        session_store: "SessionStore",
        observations: ObservationStore,
        context_manager: ContextManager,
        turn_offset: int = 0,
        reconciliation_required: bool = False,
        reconciliation_start_index: int = 0,
        event_bus: EventBus | None = None,
        trace_context: TraceContext | None = None,
    ) -> None:
        self.state = state
        self.session_store = session_store
        self.observations = observations
        self.context_manager = context_manager
        self.turn_offset = turn_offset
        self.reconciliation_required = reconciliation_required
        self.reconciliation_start_index = reconciliation_start_index
        self.reconciled = not reconciliation_required
        self.event_bus = event_bus
        self.trace_context = trace_context

    async def on_context_updated(
        self,
        context: Context,
        *,
        turn_index: int,
    ) -> None:
        self.state.turn_count = max(
            self.state.turn_count,
            self.turn_offset + turn_index,
        )
        self.state.updated_at = _now()
        self.state.observations = [
            item.model_copy(deep=True)
            for item in self.observations.all()
        ]
        structurally_interrupted = context_is_interrupted(context)
        if (
            self.reconciliation_required
            and _has_reconciliation_evidence(
                context,
                start_index=self.reconciliation_start_index,
            )
        ):
            self.reconciled = True

        self.state.interrupted = (
            structurally_interrupted or not self.reconciled
        )

        # Persist the exact latest context first.  An incomplete tool group is
        # intentionally marked interrupted and never sent to the compactor.
        self._save(context)
        await self._emit(AgentEventType.SESSION_SAVED, {"turn_index": turn_index})

        if self.state.interrupted:
            return

        prepared = self.context_manager.prepare(
            context=context,
            session=self.state,
            observations=self.observations,
        )

        if prepared is not context:
            context.system_prompt = prepared.system_prompt
            context.messages = prepared.messages
            self.state.updated_at = _now()
            self._save(context)
            await self._emit(
                AgentEventType.CONTEXT_COMPACTED,
                {
                    "compaction_count": self.state.compaction_count,
                    "turn_count": self.state.turn_count,
                },
            )

    async def _emit(self, event_type: AgentEventType, payload: dict) -> None:
        if self.event_bus is None or self.trace_context is None:
            return
        await self.event_bus.emit(
            self.trace_context.child().event(
                session_id=self.state.session_id,
                type=event_type,
                payload=payload,
            )
        )

    def _save(self, context: Context) -> None:
        self.session_store.save(
            SessionSnapshot(
                state=self.state,
                context=context,
            )
        )


class CodingSession:
    """Outer V6 lifecycle around the unchanged model/tool AgentLoop."""

    def __init__(
        self,
        *,
        provider: Provider,
        model: Model,
        tool_dispatcher: ToolDispatcher,
        session_store: "SessionStore",
        context_manager: ContextManager,
        snapshot: SessionSnapshot,
        observations: ObservationStore,
        max_turns: int = 20,
        event_bus: EventBus | None = None,
        trace_context: TraceContext | None = None,
        resumed: bool = False,
    ) -> None:
        self.provider = provider
        self.model = model
        self.tool_dispatcher = tool_dispatcher
        self.session_store = session_store
        self.context_manager = context_manager
        self.snapshot = snapshot
        self.observations = observations
        self.max_turns = max_turns
        self.event_bus = event_bus
        self.trace_context = trace_context
        self.resumed = resumed
        bind_observation_runtime(tool_dispatcher, observations)
        self._restore_permission_state()

    @classmethod
    def create(
        cls,
        *,
        user_goal: str,
        provider: Provider,
        model: Model,
        tool_dispatcher: ToolDispatcher,
        session_store: "SessionStore",
        context_manager: ContextManager,
        system_prompt: str = "",
        observations: ObservationStore | None = None,
        max_turns: int = 20,
        session_id: str | None = None,
        event_bus: EventBus | None = None,
        trace_context: TraceContext | None = None,
    ) -> "CodingSession":
        observation_store = observations or ObservationStore()
        update_request = getattr(
            tool_dispatcher.permission_engine,
            "update_user_request",
            None,
        )
        if callable(update_request):
            update_request(user_goal)
        permission_engine = tool_dispatcher.permission_engine
        state = SessionState(
            session_id=session_id or str(uuid4()),
            user_goal=user_goal,
            permission_mode=getattr(
                permission_engine, "mode", PermissionMode.DEFAULT,
            ),
            safety_constraints=getattr(
                permission_engine, "safety_constraints", SafetyConstraints(),
            ),
        )
        snapshot = SessionSnapshot(
            state=state,
            context=Context(
                system_prompt=_ensure_runtime_prompt(system_prompt),
            ),
        )
        session_store.save(snapshot)
        return cls(
            provider=provider,
            model=model,
            tool_dispatcher=tool_dispatcher,
            session_store=session_store,
            context_manager=context_manager,
            snapshot=snapshot,
            observations=observation_store,
            max_turns=max_turns,
            event_bus=event_bus,
            trace_context=trace_context,
        )

    @classmethod
    def resume(
        cls,
        session_id: str,
        *,
        provider: Provider,
        model: Model,
        tool_dispatcher: ToolDispatcher,
        session_store: "SessionStore",
        context_manager: ContextManager,
        observations: ObservationStore | None = None,
        max_turns: int = 20,
        event_bus: EventBus | None = None,
        trace_context: TraceContext | None = None,
    ) -> "CodingSession":
        snapshot = session_store.load(session_id)
        observation_store = observations or ObservationStore()
        observation_store.replace_all(
            [
                item.model_copy(deep=True)
                for item in snapshot.state.observations
            ]
        )

        interrupted = (
            snapshot.state.interrupted
            or context_is_interrupted(snapshot.context)
        )

        if interrupted:
            snapshot.state.interrupted = True
            snapshot.context = stable_context_prefix(snapshot.context)
            snapshot.context.system_prompt = _ensure_interrupted_prompt(
                snapshot.context.system_prompt
            )

        snapshot.context.system_prompt = _ensure_runtime_prompt(
            snapshot.context.system_prompt
        )
        return cls(
            provider=provider,
            model=model,
            tool_dispatcher=tool_dispatcher,
            session_store=session_store,
            context_manager=context_manager,
            snapshot=snapshot,
            observations=observation_store,
            max_turns=max_turns,
            event_bus=event_bus,
            trace_context=trace_context,
            resumed=True,
        )

    @property
    def session_id(self) -> str:
        return self.snapshot.state.session_id

    @property
    def state(self) -> SessionState:
        return self.snapshot.state

    @property
    def context(self) -> Context:
        return self.snapshot.context

    async def run(self, user_message: str) -> AssistantMessage:
        if not user_message:
            raise ValueError("user_message 不能为空")

        state = self.state
        context = self.context
        state.status = SessionStatus.ACTIVE
        state.updated_at = _now()
        reconciliation_required = state.interrupted
        turn_offset = state.turn_count
        context.messages.append(UserMessage(content=user_message))
        update_request = getattr(
            self.tool_dispatcher.permission_engine,
            "update_user_request",
            None,
        )
        if callable(update_request):
            update_request(f"{state.user_goal}\n{user_message}")
        self._sync_permission_state()
        self._sync_observations()
        self._save()
        await self._emit_session(
            AgentEventType.SESSION_RESUMED
            if self.resumed
            else AgentEventType.SESSION_SAVED,
            {"turn_count": state.turn_count},
        )
        self.resumed = False

        prepared = self.context_manager.prepare(
            context=context,
            session=state,
            observations=self.observations,
        )

        if prepared is not context:
            context.system_prompt = prepared.system_prompt
            context.messages = prepared.messages
            self._save()
            await self._emit_session(
                AgentEventType.CONTEXT_COMPACTED,
                {
                    "compaction_count": state.compaction_count,
                    "turn_count": state.turn_count,
                },
            )

        reconciliation_start_index = len(context.messages)

        observer = SessionPersistenceObserver(
            state=state,
            session_store=self.session_store,
            observations=self.observations,
            context_manager=self.context_manager,
            turn_offset=turn_offset,
            reconciliation_required=reconciliation_required,
            reconciliation_start_index=reconciliation_start_index,
            event_bus=self.event_bus,
            trace_context=self.trace_context,
        )

        try:
            result = await run_agent_loop(
                provider=self.provider,
                model=self.model,
                context=context,
                tool_dispatcher=self.tool_dispatcher,
                max_turns=self.max_turns,
                observer=observer,
            )
        except asyncio.CancelledError:
            state.status = SessionStatus.CANCELLED
            state.interrupted = (
                context_is_interrupted(context)
                or not observer.reconciled
            )
            state.updated_at = _now()
            self._sync_observations()
            self._sync_permission_state()
            self._save()
            raise
        except Exception:
            state.status = SessionStatus.FAILED
            state.interrupted = (
                context_is_interrupted(context)
                or not observer.reconciled
            )
            state.updated_at = _now()
            self._sync_observations()
            self._sync_permission_state()
            self._save()
            raise

        if state.interrupted:
            state.status = SessionStatus.ACTIVE
            state.updated_at = _now()
            self._sync_observations()
            self._sync_permission_state()
            self._save()
            raise SessionReconciliationRequiredError(
                session_id=state.session_id
            )

        state.status = SessionStatus.COMPLETED
        state.updated_at = _now()
        self._sync_observations()
        self._sync_permission_state()
        self._save()
        return result

    def _restore_permission_state(self) -> None:
        engine = self.tool_dispatcher.permission_engine
        if hasattr(engine, "mode"):
            engine.mode = self.state.permission_mode
        if hasattr(engine, "safety_constraints"):
            engine.safety_constraints = self.state.safety_constraints.model_copy(deep=True)
        if hasattr(engine, "auto_consecutive_blocks"):
            engine.auto_consecutive_blocks = self.state.auto_consecutive_blocks
        if hasattr(engine, "auto_total_blocks"):
            engine.auto_total_blocks = self.state.auto_total_blocks

    def _sync_permission_state(self) -> None:
        engine = self.tool_dispatcher.permission_engine
        if hasattr(engine, "mode"):
            self.state.permission_mode = engine.mode
        if hasattr(engine, "safety_constraints"):
            self.state.safety_constraints = engine.safety_constraints.model_copy(deep=True)
        if hasattr(engine, "auto_consecutive_blocks"):
            self.state.auto_consecutive_blocks = engine.auto_consecutive_blocks
        if hasattr(engine, "auto_total_blocks"):
            self.state.auto_total_blocks = engine.auto_total_blocks

    async def _emit_session(
        self,
        event_type: AgentEventType,
        payload: dict,
    ) -> None:
        if self.event_bus is None or self.trace_context is None:
            return
        await self.event_bus.emit(
            self.trace_context.child().event(
                session_id=self.session_id,
                type=event_type,
                payload=payload,
            )
        )

    def record_plan(
        self,
        plan: TaskPlan,
        *,
        replan_reason: str | None = None,
    ) -> None:
        """Atomically update the persisted Host-owned plan state."""

        self.state.current_plan = plan.model_copy(deep=True)
        self.state.plan_revision = plan.revision
        if replan_reason is not None:
            self.state.latest_replan_reason = replan_reason
        self.state.updated_at = _now()
        self._save()

    def persist_runtime_state(
        self,
        *,
        status: SessionStatus | None = None,
    ) -> None:
        """Persist non-AgentLoop orchestration state and observations."""

        if status is not None:
            self.state.status = status
        self.state.updated_at = _now()
        self._sync_observations()
        self._sync_permission_state()
        self._save()

    def _sync_observations(self) -> None:
        self.state.observations = [
            item.model_copy(deep=True)
            for item in self.observations.all()
        ]

    def _save(self) -> None:
        self.session_store.save(self.snapshot)


def bind_observation_runtime(
    dispatcher: ToolDispatcher,
    observations: ObservationStore,
) -> None:
    """Bind result ingestion and Host version resolution to one store."""

    coding_observer = CodingObservationObserver(observations)
    existing = dispatcher.result_observer

    if existing is None or isinstance(existing, CodingObservationObserver):
        dispatcher.set_result_observer(coding_observer)
    elif isinstance(existing, _ChainedToolResultObserver):
        retained = [
            observer
            for observer in existing.observers
            if not isinstance(observer, CodingObservationObserver)
        ]
        dispatcher.set_result_observer(
            _ChainedToolResultObserver(
                [coding_observer, *retained]
            )
        )
    else:
        dispatcher.set_result_observer(
            _ChainedToolResultObserver([coding_observer, existing])
        )

    resolver = VersionResolver(observations)

    for tool in dispatcher.registry.tools:
        if hasattr(tool, "version_resolver"):
            tool.version_resolver = resolver


class _ChainedToolResultObserver:
    def __init__(self, observers: list[Any]) -> None:
        self.observers = observers

    async def on_tool_result(
        self,
        *,
        tool_name: str,
        arguments: dict,
        result: AgentToolResult,
    ) -> None:
        for observer in self.observers:
            await observer.on_tool_result(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
            )


def context_is_interrupted(context: Context) -> bool:
    """Return true for pending or malformed tool-call history."""

    pending: set[str] = set()
    malformed = False

    for message in context.messages:
        if isinstance(message, AssistantMessage) and message.tool_calls:
            ids = [call.id for call in message.tool_calls]

            if pending or len(ids) != len(set(ids)):
                malformed = True

            pending = set(ids)
        elif isinstance(message, ToolResultMessage):
            if message.tool_call_id not in pending:
                malformed = True
            else:
                pending.remove(message.tool_call_id)
        elif pending:
            malformed = True

    return malformed or bool(pending)


def _has_reconciliation_evidence(
    context: Context,
    *,
    start_index: int,
) -> bool:
    inspection_tools = {
        "read",
        "glob",
        "search",
        "powershell",
    }
    return any(
        isinstance(message, ToolResultMessage)
        and message.name in inspection_tools
        for message in context.messages[start_index:]
    )


def stable_context_prefix(context: Context) -> Context:
    """Drop an incomplete tool group without replaying any side effect."""

    stable: list[Any] = []
    index = 0

    while index < len(context.messages):
        message = context.messages[index]

        if isinstance(message, ToolResultMessage):
            break

        if isinstance(message, AssistantMessage) and message.tool_calls:
            expected = {call.id for call in message.tool_calls}
            result_slice = context.messages[
                index + 1:index + 1 + len(expected)
            ]

            if (
                len(expected) != len(message.tool_calls)
                or len(result_slice) != len(expected)
                or not all(
                    isinstance(item, ToolResultMessage)
                    for item in result_slice
                )
                or {
                    item.tool_call_id
                    for item in result_slice
                    if isinstance(item, ToolResultMessage)
                } != expected
            ):
                break

            stable.extend([message, *result_slice])
            index += 1 + len(expected)
            continue

        stable.append(message)
        index += 1

    return Context(
        system_prompt=context.system_prompt,
        messages=stable,
    )


def _ensure_runtime_prompt(system_prompt: str) -> str:
    if V6_RUNTIME_PROMPT in system_prompt:
        return system_prompt

    if system_prompt.strip():
        return f"{system_prompt.rstrip()}\n\n{V6_RUNTIME_PROMPT}"

    return V6_RUNTIME_PROMPT


def _ensure_interrupted_prompt(system_prompt: str) -> str:
    if INTERRUPTED_SESSION_PROMPT in system_prompt:
        return system_prompt

    if system_prompt.strip():
        return f"{system_prompt.rstrip()}\n\n{INTERRUPTED_SESSION_PROMPT}"

    return INTERRUPTED_SESSION_PROMPT
