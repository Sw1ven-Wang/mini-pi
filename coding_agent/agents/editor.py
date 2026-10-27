"""Single-model V7 editor that emits Change IR without mutating files."""

from __future__ import annotations

import json

from pydantic import ValidationError

from ai.context import Context
from ai.events import StreamEnd, StreamError
from ai.messages import AssistantMessage, UserMessage
from ai.models import Model
from ai.providers.base import Provider
from coding_agent.agents.models import EditorRequest
from coding_agent.changes.workspace_ir import WorkspaceChangeSet
from coding_agent.diagnostics.models import Diagnostic
from coding_agent.errors import EditingError


EDITOR_SYSTEM_PROMPT = """You are the editing phase of a coding runtime.
Use only the supplied semantic ChangeIntent, version-bound EvidenceBundle, and
current diagnostics. Return exactly one JSON WorkspaceChangeSet. Never claim
that a mutation was already applied and never call tools. Select operations by
these deterministic host conventions: edit_file for existing text, create_file
for a new file, delete_file for deletion, and move_file for rename/move.
Use exact, unique old_text copied from evidence. The host supplies authoritative
expected versions, so omit version fields. Shape:
{"operations":[
  {"kind":"edit_file","path":"relative.py","replacements":[
    {"old_text":"exact evidence","new_text":"replacement"}]},
  {"kind":"create_file","path":"relative.py","content":"..."},
  {"kind":"delete_file","path":"relative.py"},
  {"kind":"move_file","source":"old.py","destination":"new.py"}
]}
Only include operations required by the intent."""


class Editor:
    """Generate validated Change IR using the same configured model as Planner."""

    def __init__(self, provider: Provider, model: Model) -> None:
        self.provider = provider
        self.model = model

    async def create_change_set(
        self,
        *,
        request: EditorRequest,
        diagnostics: list[Diagnostic] | None = None,
    ) -> WorkspaceChangeSet:
        payload = {
            "request": request.model_dump(mode="json"),
            "diagnostics": [
                item.model_dump(mode="json") for item in (diagnostics or [])
            ],
        }
        context = Context(
            system_prompt=EDITOR_SYSTEM_PROMPT,
            messages=[
                UserMessage(
                    content=json.dumps(payload, ensure_ascii=False, sort_keys=True)
                )
            ],
        )
        assistant = await self._complete(context)

        try:
            raw = _parse_json_object(assistant.content)
            _inject_versions(raw, request)
            change_set = WorkspaceChangeSet.model_validate(raw)
            self._validate_scope(change_set, request)
            self._validate_replacements(change_set, request)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise EditingError(
                f"Editor 输出不是安全的 WorkspaceChangeSet: {exc}"
            ) from exc

        if not change_set.operations:
            raise EditingError("Editor 输出了空 WorkspaceChangeSet")

        return change_set

    async def _complete(self, context: Context) -> AssistantMessage:
        final: AssistantMessage | None = None
        async for event in self.provider.stream(self.model, context, []):
            if isinstance(event, StreamError):
                raise EditingError(f"Editor model 调用失败: {event.error}")
            if isinstance(event, StreamEnd):
                final = event.message

        if final is None:
            raise EditingError("Editor model 未返回完整响应")
        if final.tool_calls:
            raise EditingError("Editor 不允许直接调用 mutation 工具")
        return final

    @staticmethod
    def _validate_scope(
        change_set: WorkspaceChangeSet,
        request: EditorRequest,
    ) -> None:
        evidence_paths = {item.path for item in request.evidence.snippets}
        allowed_paths = evidence_paths | set(request.intent.target_files)

        for operation in change_set.operations:
            paths = []
            if operation.kind in {"create_file", "edit_file", "delete_file"}:
                paths.append(operation.path)
            else:
                paths.extend([operation.source, operation.destination])
            outside = [path for path in paths if path not in allowed_paths]
            if outside:
                raise ValueError(
                    "operation 超出 intent/evidence 范围: " + ", ".join(outside)
                )

            if operation.kind in {"edit_file", "delete_file"}:
                if operation.path not in evidence_paths:
                    raise ValueError(f"mutation 缺少已读 evidence: {operation.path}")
            elif operation.kind == "move_file" and operation.source not in evidence_paths:
                raise ValueError(f"move source 缺少已读 evidence: {operation.source}")

    @staticmethod
    def _validate_replacements(
        change_set: WorkspaceChangeSet,
        request: EditorRequest,
    ) -> None:
        by_path: dict[str, str] = {}
        for snippet in request.evidence.snippets:
            by_path[snippet.path] = by_path.get(snippet.path, "") + snippet.content

        for operation in change_set.operations:
            if operation.kind != "edit_file":
                continue
            evidence = by_path.get(operation.path, "")
            for replacement in operation.replacements:
                if not replacement.old_text:
                    raise ValueError("old_text 不能为空")
                if replacement.old_text not in evidence:
                    raise ValueError(
                        f"old_text 不在 version-bound evidence 中: {operation.path}"
                    )


def _inject_versions(payload: dict, request: EditorRequest) -> None:
    operations = payload.get("operations")
    if not isinstance(operations, list):
        return

    versions = {item.path: item.version for item in request.evidence.snippets}
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        kind = operation.get("kind")
        if kind in {"edit_file", "delete_file"}:
            version = versions.get(operation.get("path"))
            if version is not None:
                operation["expected_version"] = version.model_dump(mode="json")
        elif kind == "move_file":
            version = versions.get(operation.get("source"))
            if version is not None:
                operation["expected_source_version"] = version.model_dump(mode="json")


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
        raise ValueError("Editor JSON 顶层必须是 object")
    return payload
