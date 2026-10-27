"""Single-model V7 planner with no tool access."""

from __future__ import annotations

import json

from pydantic import ValidationError

from ai.context import Context
from ai.events import StreamEnd, StreamError
from ai.messages import AssistantMessage, UserMessage
from ai.models import Model
from ai.providers.base import Provider
from coding_agent.agents.models import PlannerContext, TaskPlan
from coding_agent.errors import PlanningError


PLANNER_SYSTEM_PROMPT = """You are the planning phase of a coding runtime.
Use only the supplied goal, constraints, repository summary, session summary,
and diagnostics. Produce a finite, executable JSON TaskPlan with no more than
10 steps. Define evidence needs and acceptance criteria. Do not emit patches,
old_text/new_text, shell commands, or claim that files were changed.
Return exactly one JSON object matching this shape:
{
  "objective": "...",
  "constraints": ["..."],
  "steps": [{
    "id": "step-1",
    "objective": "...",
    "target_files": ["relative/path.py"],
    "evidence_queries": ["symbol or literal"],
    "acceptance_criteria": ["..."]
  }],
  "acceptance_criteria": ["..."],
  "revision": 1
}"""


class Planner:
    """Create validated plans using the configured shared model."""

    def __init__(self, provider: Provider, model: Model) -> None:
        self.provider = provider
        self.model = model

    async def create_plan(
        self,
        *,
        goal: str,
        context: PlannerContext,
    ) -> TaskPlan:
        if not goal:
            raise PlanningError("goal 不能为空")

        prompt_payload = {
            "goal": goal,
            **context.model_dump(mode="json"),
        }
        isolated_context = Context(
            system_prompt=PLANNER_SYSTEM_PROMPT,
            messages=[
                UserMessage(
                    content=json.dumps(
                        prompt_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            ],
        )
        assistant = await self._complete(isolated_context)

        try:
            payload = _parse_json_object(assistant.content)
            plan = TaskPlan.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise PlanningError(
                f"Planner 输出不是合法 TaskPlan: {exc}"
            ) from exc

        # The runtime, not the model, owns the initial revision.
        if plan.revision != 1:
            plan = plan.model_copy(update={"revision": 1})

        return plan

    async def _complete(self, context: Context) -> AssistantMessage:
        final: AssistantMessage | None = None

        # Planner has no tools. This is the phase permission boundary.
        async for event in self.provider.stream(
            self.model,
            context,
            [],
        ):
            if isinstance(event, StreamError):
                raise PlanningError(
                    f"Planner model 调用失败: {event.error}"
                )

            if isinstance(event, StreamEnd):
                final = event.message

        if final is None:
            raise PlanningError("Planner model 未返回完整响应")

        if final.tool_calls:
            raise PlanningError("Planner 不允许调用工具")

        return final


def _parse_json_object(content: str) -> dict:
    text = content.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])

            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:].lstrip()

    payload = json.loads(text)

    if not isinstance(payload, dict):
        raise ValueError("Planner JSON 顶层必须是 object")

    return payload
