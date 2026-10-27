"""FastAPI application factory for the local Mini-Pi workbench."""

from __future__ import annotations

import hmac
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from coding_agent.web.config import AllowedRoot, ProviderRegistry
from coding_agent.web.directories import DirectoryBrowser
from coding_agent.web.models import (
    ApprovalDecisionRequest,
    AcceptanceConfirmationRequest,
    ReconciliationRequest,
    BootstrapResponse,
    ChangePreviewResponse,
    DirectoryListing,
    ResumeRequest,
    SessionSummary,
    TaskCreateRequest,
    TaskSnapshot,
)
from coding_agent.web.tasks import (
    ActiveTaskError,
    TaskManager,
    TaskNotFoundError,
    TaskStateError,
)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CSRF_COOKIE = "mini_pi_csrf"
_CSRF_HEADER = "x-mini-pi-csrf"


def create_app(
    *,
    roots: list[AllowedRoot],
    providers: ProviderRegistry,
    verify_command: str,
    static_dir: Path | None = None,
    csrf_token: str | None = None,
    task_manager: TaskManager | None = None,
    state_root: Path | None = None,
    acceptance_contract=None,
) -> FastAPI:
    directories = DirectoryBrowser(roots)
    manager = task_manager or TaskManager(
        directories=directories,
        providers=providers,
        verify_command=verify_command,
        state_root=state_root,
        acceptance_contract=acceptance_contract,
    )
    token = csrf_token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(
        title="Mini-Pi Workbench",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.task_manager = manager
    app.state.csrf_token = token

    @app.middleware("http")
    async def local_security_boundary(request: Request, call_next):
        if not _is_local_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "non-local Host is forbidden"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and not _is_local_origin(origin):
            return JSONResponse({"detail": "non-local Origin is forbidden"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/bootstrap":
            cookie = request.cookies.get(_CSRF_COOKIE)
            if not cookie or not hmac.compare_digest(cookie, token):
                return JSONResponse({"detail": "workbench session is not initialized"}, status_code=403)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get(_CSRF_HEADER, "")
                if not supplied or not hmac.compare_digest(supplied, token):
                    return JSONResponse({"detail": "invalid CSRF token"}, status_code=403)
        return await call_next(request)

    @app.get("/api/bootstrap", response_model=BootstrapResponse)
    async def bootstrap(response: Response) -> BootstrapResponse:
        response.set_cookie(
            _CSRF_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return BootstrapResponse(
            csrf_token=token,
            roots=[root.public() for root in directories.roots],
            providers=providers.public_profiles(),
            verify_command=verify_command,
            active_session_id=manager.active_session_id,
        )

    @app.get("/api/directories", response_model=DirectoryListing)
    async def list_directories(
        root_id: str = Query(min_length=1, max_length=128),
        path: str = Query(default="", max_length=2048),
    ) -> DirectoryListing:
        try:
            return directories.browse(root_id, path)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="allowed root not found") from exc
        except (ValueError, PermissionError, OSError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/tasks", response_model=TaskSnapshot, status_code=201)
    async def create_task(payload: TaskCreateRequest) -> TaskSnapshot:
        try:
            return await manager.create(payload)
        except ActiveTaskError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="provider or root not found") from exc
        except (ValueError, PermissionError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/tasks/{session_id}", response_model=TaskSnapshot)
    async def get_task(session_id: str) -> TaskSnapshot:
        try:
            return manager.snapshot(session_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc

    @app.get("/api/tasks/{session_id}/events")
    async def task_events(
        session_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            sink = manager.event_sink(session_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        try:
            after = int(last_event_id or "0")
            if after < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Last-Event-ID") from exc

        async def stream():
            async for sequence, event in sink.stream(
                after, heartbeat_seconds=15,
            ):
                if event is None:
                    yield ": keepalive\n\n"
                    continue
                data = event.model_dump(mode="json")
                yield (
                    f"id: {sequence}\n"
                    "event: agent\n"
                    f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        "/api/tasks/{session_id}/changes",
        response_model=ChangePreviewResponse,
    )
    async def task_changes(
        session_id: str,
        preview_id: str | None = Query(default=None, max_length=128),
    ) -> ChangePreviewResponse:
        try:
            return ChangePreviewResponse(
                preview=manager.preview(session_id, preview_id),
            )
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc

    @app.post(
        "/api/tasks/{session_id}/approvals/{approval_id}",
        response_model=TaskSnapshot,
    )
    async def resolve_approval(
        session_id: str,
        approval_id: str,
        payload: ApprovalDecisionRequest,
    ) -> TaskSnapshot:
        try:
            manager.approve(session_id, approval_id, payload.decision, payload.expected_revision)
            return manager.snapshot(session_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except TaskStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{session_id}/acceptance/{requirement_id}", response_model=TaskSnapshot)
    async def confirm_acceptance(session_id: str, requirement_id: str, payload: AcceptanceConfirmationRequest):
        try:
            return manager.confirm_acceptance(session_id, requirement_id, payload.expected_revision)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{session_id}/cancel", response_model=TaskSnapshot)
    async def cancel_task(session_id: str) -> TaskSnapshot:
        try:
            return await manager.cancel(session_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except TaskStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{session_id}/reconcile", response_model=TaskSnapshot)
    async def reconcile_task(session_id: str, payload: ReconciliationRequest):
        try:
            return await manager.reconcile(session_id, payload.action, payload.expected_revision)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except TaskStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/sessions", response_model=list[SessionSummary])
    async def list_sessions(
        root_id: str = Query(min_length=1, max_length=128),
        workspace_path: str = Query(default="", max_length=2048),
    ) -> list[SessionSummary]:
        try:
            return manager.list_sessions(root_id, workspace_path)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="allowed root not found") from exc
        except (ValueError, PermissionError, OSError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/resume", response_model=TaskSnapshot)
    async def resume_session(
        session_id: str,
        payload: ResumeRequest,
    ) -> TaskSnapshot:
        try:
            return await manager.resume(
                session_id=session_id,
                root_id=payload.root_id,
                workspace_path=payload.workspace_path,
            )
        except ActiveTaskError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TaskNotFoundError, KeyError) as exc:
            raise HTTPException(status_code=404, detail="session or configuration not found") from exc
        except (TaskStateError, ValueError, PermissionError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    static_root = Path(static_dir).resolve() if static_dir is not None else None
    if static_root is not None and (static_root / "assets").is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=static_root / "assets"),
            name="assets",
        )

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        if static_root is None or not (static_root / "index.html").is_file():
            raise HTTPException(status_code=404, detail="web build is not available")
        requested = (static_root / path).resolve(strict=False)
        if path and requested.is_relative_to(static_root) and requested.is_file():
            return FileResponse(requested)
        return FileResponse(static_root / "index.html")

    return app


def _is_local_host(value: str) -> bool:
    try:
        return urlsplit(f"//{value}").hostname in _LOCAL_HOSTS
    except ValueError:
        return False


def _is_local_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in _LOCAL_HOSTS
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False
