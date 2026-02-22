from dataclasses import asdict
import os
from pathlib import Path
from typing import Any, Dict, List, Set

from pydantic import BaseModel
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.services.error_codes import ERROR_CATALOG, detect_error_code, get_catalog_entry
from codex_telegram_bot.services.onboarding import OnboardingStore
from codex_telegram_bot.config import get_env_path, load_env_file, write_env_file


class HandoffRequest(BaseModel):
    from_agent_id: str
    to_agent_id: str
    prompt: str
    parent_run_id: str = ""


class RecoveryRequest(BaseModel):
    action_id: str


class ApproveToolRequest(BaseModel):
    approval_id: str
    chat_id: int
    user_id: int


class LocalApiPromptRequest(BaseModel):
    prompt: str
    agent_id: str = "default"


def _run_to_dict(run) -> Dict[str, Any]:
    data = asdict(run)
    for key in ("created_at", "started_at", "completed_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    if data.get("error"):
        code = detect_error_code(data["error"])
        catalog = get_catalog_entry(code)
        data["error_code"] = code
        data["error_title"] = catalog.title
        data["error_message"] = catalog.user_message
    else:
        data["error_code"] = ""
        data["error_title"] = ""
        data["error_message"] = ""
    return data


def _event_to_dict(event) -> Dict[str, Any]:
    return {
        "run_id": event.run_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


def _catalog_to_dict() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in ERROR_CATALOG:
        out.append(
            {
                "code": entry.code,
                "title": entry.title,
                "user_message": entry.user_message,
                "actions": [
                    {
                        "action_id": action.action_id,
                        "label": action.label,
                        "description": action.description,
                    }
                    for action in entry.actions
                ],
            }
        )
    return out


def _allowed_recovery_actions() -> Dict[str, str]:
    return {
        "retry_same_agent": "Queue the same prompt on the original agent.",
        "retry_default_agent": "Queue the same prompt on the default agent.",
        "open_settings": "Open settings page for provider diagnostics.",
        "open_agents": "Open agents page for profile/provider fixes.",
        "download_artifact": "Download run artifact for triage.",
    }


def _parse_local_api_keys(raw: str) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for chunk in (raw or "").split(";"):
        value = chunk.strip()
        if not value:
            continue
        parts = value.split(":", 1)
        if len(parts) != 2:
            continue
        token = parts[0].strip()
        scopes_raw = parts[1].strip()
        if not token:
            continue
        scopes = {s.strip().lower() for s in scopes_raw.split(",") if s.strip()}
        if not scopes:
            scopes = {"runs:read"}
        out[token] = scopes
    return out


def create_app(agent_service: AgentService) -> FastAPI:
    return create_app_with_config(agent_service=agent_service, config_dir=None)


def create_app_with_config(agent_service: AgentService, config_dir: Path | None) -> FastAPI:
    base_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(base_dir / "templates"))
    app = FastAPI(title="Codex Control Center", version="0.2.0-alpha")
    app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")
    onboarding = OnboardingStore(config_dir=config_dir)
    local_api_version = "v1"
    local_api_keys = _parse_local_api_keys(os.environ.get("LOCAL_API_KEYS", ""))
    local_api_enabled = bool(local_api_keys)

    def _resolve_api_token(request: Request) -> str:
        bearer = (request.headers.get("authorization") or "").strip()
        if bearer.lower().startswith("bearer "):
            return bearer[7:].strip()
        return (request.headers.get("x-local-api-key") or "").strip()

    def _require_local_api_scope(request: Request, required_scope: str) -> str:
        if not local_api_enabled:
            raise HTTPException(status_code=503, detail="Local integration API disabled.")
        token = _resolve_api_token(request)
        if not token:
            raise HTTPException(status_code=401, detail="Missing local API token.")
        scopes = local_api_keys.get(token)
        if not scopes:
            raise HTTPException(status_code=401, detail="Invalid local API token.")
        if "admin:*" in scopes or required_scope in scopes:
            return token
        raise HTTPException(status_code=403, detail=f"Missing scope: {required_scope}")

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        metrics = agent_service.metrics()
        reliability = agent_service.reliability_snapshot(limit=300)
        provider_version = await agent_service.provider_version()
        provider_health = await agent_service.provider_health()
        return {
            "status": "ok",
            "provider_version": provider_version,
            "provider_health": provider_health,
            "metrics": metrics,
            "reliability": reliability,
        }

    @app.get("/api/metrics")
    async def api_metrics() -> Dict[str, int]:
        return agent_service.metrics()

    @app.get("/api/reliability")
    async def api_reliability(limit: int = 500) -> Dict[str, Any]:
        return agent_service.reliability_snapshot(limit=max(10, min(limit, 5000)))

    @app.get("/api/onboarding/status")
    async def onboarding_status() -> Dict[str, Any]:
        return onboarding.load()

    @app.get("/api/runs")
    async def list_runs(limit: int = 20) -> List[Dict[str, Any]]:
        runs = agent_service.list_recent_runs(limit=max(1, min(limit, 100)))
        return [_run_to_dict(r) for r in runs]

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> Dict[str, Any]:
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_to_dict(run)

    @app.get("/api/error-catalog")
    async def api_error_catalog() -> List[Dict[str, Any]]:
        return _catalog_to_dict()

    @app.get("/api/runs/{run_id}/recovery-options")
    async def get_recovery_options(run_id: str) -> Dict[str, Any]:
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if not run.error:
            return {"run_id": run_id, "error_code": "", "actions": []}
        code = detect_error_code(run.error)
        entry = get_catalog_entry(code)
        actions = [
            {
                "action_id": a.action_id,
                "label": a.label,
                "description": a.description,
                "kind": "api" if a.action_id.startswith("retry_") else "link",
            }
            for a in entry.actions
        ]
        return {"run_id": run_id, "error_code": code, "actions": actions}

    @app.get("/api/runs/{run_id}/events")
    async def get_run_events(run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        events = agent_service.list_run_events(run_id=run_id, limit=max(1, min(limit, 500)))
        return [_event_to_dict(e) for e in events]

    @app.get("/api/runs/{run_id}/artifact.txt")
    async def download_run_artifact(run_id: str) -> PlainTextResponse:
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        events = agent_service.list_run_events(run_id=run_id, limit=500)

        body = []
        body.append(f"Run ID: {run.run_id}")
        body.append(f"Status: {run.status}")
        body.append(f"Created: {run.created_at.isoformat()}")
        body.append(f"Started: {run.started_at.isoformat() if run.started_at else '-'}")
        body.append(f"Completed: {run.completed_at.isoformat() if run.completed_at else '-'}")
        body.append("")
        body.append("=== Prompt ===")
        body.append(run.prompt or "")
        body.append("")
        body.append("=== Output ===")
        body.append(run.output or "")
        body.append("")
        body.append("=== Error ===")
        body.append(run.error or "")
        body.append("")
        body.append("=== Timeline ===")
        for event in events:
            body.append(f"{event.created_at.isoformat()} [{event.event_type}] {event.payload}")

        content = "\n".join(body).strip() + "\n"
        headers = {"Content-Disposition": f'attachment; filename="run-{run_id}.txt"'}
        return PlainTextResponse(content=content, headers=headers)

    @app.get("/api/agents")
    async def api_list_agents() -> List[Dict[str, Any]]:
        agents = agent_service.list_agents()
        return [
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "provider": a.provider,
                "policy_profile": a.policy_profile,
                "max_concurrency": a.max_concurrency,
                "enabled": a.enabled,
                "created_at": a.created_at.isoformat(),
                "updated_at": a.updated_at.isoformat(),
            }
            for a in agents
        ]

    @app.get("/api/sessions")
    async def api_list_sessions(limit: int = 50) -> List[Dict[str, Any]]:
        sessions = agent_service.list_recent_sessions(limit=max(1, min(limit, 200)))
        return [
            {
                "session_id": s.session_id,
                "chat_id": s.chat_id,
                "user_id": s.user_id,
                "status": s.status,
                "current_agent_id": s.current_agent_id,
                "summary": s.summary,
                "last_run_id": s.last_run_id,
                "created_at": s.created_at.isoformat(),
                "updated_at": s.updated_at.isoformat(),
            }
            for s in sessions
        ]

    @app.get("/api/approvals")
    async def api_list_approvals(limit: int = 200) -> List[Dict[str, Any]]:
        items = agent_service.list_all_pending_tool_approvals(limit=max(1, min(limit, 500)))
        return items

    @app.get("/api/retrieval")
    async def api_retrieval(query: str, limit: int = 4) -> Dict[str, Any]:
        context_lines = agent_service.build_retrieval_context(user_prompt=query, limit=max(1, min(limit, 10)))
        return {"query": query, "context": context_lines}

    @app.get("/api/retrieval/stats")
    async def api_retrieval_stats() -> Dict[str, Any]:
        return agent_service.retrieval_stats()

    @app.post("/api/retrieval/refresh")
    async def api_retrieval_refresh() -> Dict[str, Any]:
        return agent_service.refresh_retrieval_index(force=True)

    @app.post("/api/approvals/approve")
    async def api_approve_tool(req: ApproveToolRequest) -> Dict[str, Any]:
        output = await agent_service.approve_tool_action(
            approval_id=req.approval_id,
            chat_id=req.chat_id,
            user_id=req.user_id,
        )
        return {"approval_id": req.approval_id, "output": output}

    @app.post("/api/approvals/deny")
    async def api_deny_tool(req: ApproveToolRequest) -> Dict[str, Any]:
        output = agent_service.deny_tool_action(
            approval_id=req.approval_id,
            chat_id=req.chat_id,
            user_id=req.user_id,
        )
        return {"approval_id": req.approval_id, "output": output}

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_cancel_job(job_id: str) -> Dict[str, Any]:
        cancelled = agent_service.cancel_job(job_id)
        return {"job_id": job_id, "cancelled": cancelled, "status": agent_service.job_status(job_id)}

    @app.post("/api/handoffs")
    async def api_handoff(req: HandoffRequest) -> Dict[str, Any]:
        return await agent_service.handoff_prompt(
            from_agent_id=req.from_agent_id,
            to_agent_id=req.to_agent_id,
            prompt=req.prompt,
            parent_run_id=req.parent_run_id,
        )

    @app.post("/api/runs/{run_id}/recover")
    async def api_recover_run(run_id: str, req: RecoveryRequest) -> Dict[str, Any]:
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        action = (req.action_id or "").strip()
        allowed = _allowed_recovery_actions()
        if action not in allowed:
            raise HTTPException(status_code=400, detail="Unsupported recovery action")

        agent_service.append_run_event(
            run_id=run_id,
            event_type="recovery.attempted",
            payload=f"action={action}",
        )

        if action in {"open_settings", "open_agents", "download_artifact"}:
            destination = {
                "open_settings": "/settings",
                "open_agents": "/agents",
                "download_artifact": f"/api/runs/{run_id}/artifact.txt",
            }[action]
            agent_service.append_run_event(
                run_id=run_id,
                event_type="recovery.completed",
                payload=f"action={action}, destination={destination}",
            )
            return {"run_id": run_id, "status": "completed", "action_id": action, "redirect_to": destination}

        target_agent = "default" if action == "retry_default_agent" else agent_service.infer_run_agent_id(run_id)
        try:
            job_id = await agent_service.queue_prompt(prompt=run.prompt or "", agent_id=target_agent)
        except Exception as exc:
            agent_service.append_run_event(
                run_id=run_id,
                event_type="recovery.failed",
                payload=f"action={action}, reason={str(exc)[:200]}",
            )
            return {
                "run_id": run_id,
                "status": "failed",
                "action_id": action,
                "reason": "queue_failed",
            }

        agent_service.append_run_event(
            run_id=run_id,
            event_type="recovery.queued",
            payload=f"action={action}, job_id={job_id}, target_agent={target_agent}",
        )
        return {
            "run_id": run_id,
            "status": "queued",
            "action_id": action,
            "job_id": job_id,
            "target_agent": target_agent,
        }

    @app.get("/api/recovery/playbook")
    async def api_recovery_playbook() -> Dict[str, Any]:
        return {
            "actions": _allowed_recovery_actions(),
            "docs": "/docs/recovery_playbook.md",
            "notes": [
                "Use retry_same_agent for transient provider issues.",
                "Use retry_default_agent when agent-specific profile likely caused failure.",
                "Do not auto-approve high-risk tool actions during recovery.",
            ],
        }

    @app.get("/api/v1/meta")
    async def api_v1_meta(request: Request) -> Dict[str, Any]:
        _require_local_api_scope(request, "meta:read")
        return {
            "api_version": local_api_version,
            "service": "codex-telegram-bot",
            "compatibility": "minor backward compatible within v1",
            "endpoints": {
                "runs_list": {"path": "/api/v1/runs", "scope": "runs:read"},
                "runs_get": {"path": "/api/v1/runs/{run_id}", "scope": "runs:read"},
                "prompt_queue": {"path": "/api/v1/prompts", "scope": "prompts:write"},
                "job_get": {"path": "/api/v1/jobs/{job_id}", "scope": "jobs:read"},
                "job_cancel": {"path": "/api/v1/jobs/{job_id}/cancel", "scope": "jobs:write"},
            },
        }

    @app.get("/api/v1/runs")
    async def api_v1_runs(request: Request, limit: int = 20) -> Dict[str, Any]:
        _require_local_api_scope(request, "runs:read")
        runs = agent_service.list_recent_runs(limit=max(1, min(limit, 100)))
        return {"items": [_run_to_dict(r) for r in runs], "limit": limit}

    @app.get("/api/v1/runs/{run_id}")
    async def api_v1_run(request: Request, run_id: str) -> Dict[str, Any]:
        _require_local_api_scope(request, "runs:read")
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_to_dict(run)

    @app.post("/api/v1/prompts")
    async def api_v1_prompt(request: Request, req: LocalApiPromptRequest) -> Dict[str, Any]:
        _require_local_api_scope(request, "prompts:write")
        text = (req.prompt or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Prompt is required")
        job_id = await agent_service.queue_prompt(prompt=text, agent_id=(req.agent_id or "default"))
        return {"job_id": job_id, "status": agent_service.job_status(job_id), "agent_id": req.agent_id}

    @app.get("/api/v1/jobs/{job_id}")
    async def api_v1_job_status(request: Request, job_id: str) -> Dict[str, Any]:
        _require_local_api_scope(request, "jobs:read")
        return {"job_id": job_id, "status": agent_service.job_status(job_id)}

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def api_v1_job_cancel(request: Request, job_id: str) -> Dict[str, Any]:
        _require_local_api_scope(request, "jobs:write")
        cancelled = agent_service.cancel_job(job_id)
        return {"job_id": job_id, "cancelled": cancelled, "status": agent_service.job_status(job_id)}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        runs = [_run_to_dict(r) for r in agent_service.list_recent_runs(limit=30)]
        completed = len([r for r in runs if r["status"] == "completed"])
        failed = len([r for r in runs if r["status"] == "failed"])
        running = len([r for r in runs if r["status"] == "running"])
        onboarding_state = onboarding.load()
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "nav": "dashboard",
                "runs": runs,
                "onboarding_state": onboarding_state,
                "metrics": {
                    "total": len(runs),
                    "completed": completed,
                    "failed": failed,
                    "running": running,
                },
            },
        )

    @app.get("/onboarding", response_class=HTMLResponse)
    async def onboarding_page(request: Request):
        onboarding.record(step="wizard.view", outcome="visit")
        env = load_env_file(get_env_path(config_dir)) if config_dir else {}
        workspace_root = env.get("EXECUTION_WORKSPACE_ROOT", str(Path.cwd()))
        profile = "balanced"
        default_agent = agent_service.get_agent("default")
        if default_agent:
            profile = default_agent.policy_profile
        return templates.TemplateResponse(
            "onboarding.html",
            {
                "request": request,
                "nav": "onboarding",
                "onboarding_state": onboarding.load(),
                "workspace_root": workspace_root,
                "policy_profile": profile,
                "provider_key_hint": "Optional: set OPENAI_API_KEY for provider runtime.",
                "error": "",
                "result": "",
            },
        )

    @app.post("/onboarding", response_class=HTMLResponse)
    async def onboarding_submit(
        request: Request,
        provider_key: str = Form(""),
        workspace_root: str = Form(""),
        policy_profile: str = Form("balanced"),
    ):
        onboarding.record(step="wizard.submit", outcome="attempt")
        key = (provider_key or "").strip()
        workspace = Path((workspace_root or "").strip()).expanduser()
        profile = (policy_profile or "").strip().lower()

        if profile not in {"strict", "balanced", "trusted"}:
            onboarding.record(step="wizard.validate", outcome="invalid_profile")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace),
                    "policy_profile": profile,
                    "provider_key_hint": "Optional: set OPENAI_API_KEY for provider runtime.",
                    "error": "Invalid safety profile.",
                    "result": "",
                },
                status_code=400,
            )
        if not workspace.exists() or not workspace.is_dir():
            onboarding.record(step="wizard.validate", outcome="invalid_workspace")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace),
                    "policy_profile": profile,
                    "provider_key_hint": "Optional: set OPENAI_API_KEY for provider runtime.",
                    "error": "Workspace path must exist and be a directory.",
                    "result": "",
                },
                status_code=400,
            )
        if key and len(key) < 12:
            onboarding.record(step="wizard.validate", outcome="invalid_provider_key")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace),
                    "policy_profile": profile,
                    "provider_key_hint": "Optional: set OPENAI_API_KEY for provider runtime.",
                    "error": "Provider key format looks invalid.",
                    "result": "",
                },
                status_code=400,
            )

        if config_dir:
            env_path = get_env_path(config_dir)
            env = load_env_file(env_path)
            env["EXECUTION_WORKSPACE_ROOT"] = str(workspace.resolve())
            if key:
                env["OPENAI_API_KEY"] = key
            write_env_file(env_path, env)

        default_agent = agent_service.get_agent("default")
        if default_agent:
            agent_service.upsert_agent(
                agent_id=default_agent.agent_id,
                name=default_agent.name,
                provider=default_agent.provider,
                policy_profile=profile,
                max_concurrency=default_agent.max_concurrency,
                enabled=default_agent.enabled,
            )

        onboarding.record(step="wizard.validate", outcome="passed")
        test_prompt = "Reply with exactly: onboarding-ok"
        try:
            job_id = await agent_service.queue_prompt(prompt=test_prompt, agent_id="default")
            output = await agent_service.wait_job(job_id)
        except Exception as exc:
            onboarding.record(step="wizard.test_run", outcome="failed_exception")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace.resolve()),
                    "policy_profile": profile,
                    "provider_key_hint": "Optional: set OPENAI_API_KEY for provider runtime.",
                    "error": f"Test run failed: {str(exc)[:200]}",
                    "result": "",
                },
                status_code=500,
            )

        if output.startswith("Error:"):
            onboarding.record(step="wizard.test_run", outcome="failed_output")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace.resolve()),
                    "policy_profile": profile,
                    "provider_key_hint": "Optional: set OPENAI_API_KEY for provider runtime.",
                    "error": "Test run did not succeed. Review settings and try again.",
                    "result": output[:220],
                },
                status_code=400,
            )

        onboarding.record(step="wizard.test_run", outcome="passed")
        onboarding.complete()
        return templates.TemplateResponse(
            "onboarding.html",
            {
                "request": request,
                "nav": "onboarding",
                "onboarding_state": onboarding.load(),
                "workspace_root": str(workspace.resolve()),
                "policy_profile": profile,
                "provider_key_hint": "Optional: set OPENAI_API_KEY for provider runtime.",
                "error": "",
                "result": output[:220],
            },
            status_code=200,
        )

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request):
        runs = [_run_to_dict(r) for r in agent_service.list_recent_runs(limit=100)]
        return templates.TemplateResponse(
            "runs.html",
            {"request": request, "nav": "runs", "runs": runs},
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: str):
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        events = [_event_to_dict(e) for e in agent_service.list_run_events(run_id, limit=200)]
        run_dict = _run_to_dict(run)
        recovery_options = []
        if run_dict.get("error_code"):
            entry = get_catalog_entry(run_dict["error_code"])
            recovery_options = [
                {
                    "action_id": action.action_id,
                    "label": action.label,
                    "description": action.description,
                    "kind": "api" if action.action_id.startswith("retry_") else "link",
                    "href": {
                        "open_settings": "/settings",
                        "open_agents": "/agents",
                        "download_artifact": f"/api/runs/{run_id}/artifact.txt",
                    }.get(action.action_id, ""),
                }
                for action in entry.actions
            ]
        return templates.TemplateResponse(
            "run_detail.html",
            {
                "request": request,
                "nav": "runs",
                "run": run_dict,
                "events": events,
                "recovery_options": recovery_options,
            },
        )

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request, error: str = ""):
        agents = agent_service.list_agents()
        return templates.TemplateResponse(
            "agents.html",
            {"request": request, "nav": "agents", "agents": agents, "error": error},
        )

    @app.get("/sessions", response_class=HTMLResponse)
    async def sessions_page(request: Request):
        sessions = agent_service.list_recent_sessions(limit=100)
        return templates.TemplateResponse(
            "sessions.html",
            {"request": request, "nav": "sessions", "sessions": sessions},
        )

    @app.get("/approvals", response_class=HTMLResponse)
    async def approvals_page(request: Request):
        approvals = agent_service.list_all_pending_tool_approvals(limit=200)
        return templates.TemplateResponse(
            "approvals.html",
            {"request": request, "nav": "approvals", "approvals": approvals},
        )

    @app.post("/agents", response_class=HTMLResponse)
    async def create_or_update_agent(
        request: Request,
        agent_id: str = Form(...),
        name: str = Form(...),
        provider: str = Form("codex_cli"),
        policy_profile: str = Form("balanced"),
        max_concurrency: int = Form(1),
        enabled: str = Form("true"),
    ):
        try:
            agent_service.upsert_agent(
                agent_id=agent_id,
                name=name,
                provider=provider,
                policy_profile=policy_profile,
                max_concurrency=max_concurrency,
                enabled=enabled == "true",
            )
        except ValueError as exc:
            agents = agent_service.list_agents()
            return templates.TemplateResponse(
                "agents.html",
                {"request": request, "nav": "agents", "agents": agents, "error": str(exc)},
                status_code=400,
            )
        return RedirectResponse(url="/agents", status_code=303)

    @app.post("/agents/{agent_id}/delete")
    async def delete_agent(agent_id: str):
        agent_service.delete_agent(agent_id)
        return RedirectResponse(url="/agents", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        version = await agent_service.provider_version()
        provider_health = await agent_service.provider_health()
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "nav": "settings",
                "provider_version": version,
                "provider_health": provider_health,
            },
        )

    return app
