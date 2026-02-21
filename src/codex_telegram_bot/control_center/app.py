from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from codex_telegram_bot.services.agent_service import AgentService


class HandoffRequest(BaseModel):
    from_agent_id: str
    to_agent_id: str
    prompt: str
    parent_run_id: str = ""


def _run_to_dict(run) -> Dict[str, Any]:
    data = asdict(run)
    for key in ("created_at", "started_at", "completed_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


def _event_to_dict(event) -> Dict[str, Any]:
    return {
        "run_id": event.run_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


def create_app(agent_service: AgentService) -> FastAPI:
    base_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(base_dir / "templates"))
    app = FastAPI(title="Codex Control Center", version="0.2.0-alpha")
    app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        metrics = agent_service.metrics()
        provider_version = await agent_service.provider_version()
        provider_health = await agent_service.provider_health()
        return {
            "status": "ok",
            "provider_version": provider_version,
            "provider_health": provider_health,
            "metrics": metrics,
        }

    @app.get("/api/metrics")
    async def api_metrics() -> Dict[str, int]:
        return agent_service.metrics()

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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        runs = [_run_to_dict(r) for r in agent_service.list_recent_runs(limit=30)]
        completed = len([r for r in runs if r["status"] == "completed"])
        failed = len([r for r in runs if r["status"] == "failed"])
        running = len([r for r in runs if r["status"] == "running"])
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "nav": "dashboard",
                "runs": runs,
                "metrics": {
                    "total": len(runs),
                    "completed": completed,
                    "failed": failed,
                    "running": running,
                },
            },
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
        return templates.TemplateResponse(
            "run_detail.html",
            {"request": request, "nav": "runs", "run": _run_to_dict(run), "events": events},
        )

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request, error: str = ""):
        agents = agent_service.list_agents()
        return templates.TemplateResponse(
            "agents.html",
            {"request": request, "nav": "agents", "agents": agents, "error": error},
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
