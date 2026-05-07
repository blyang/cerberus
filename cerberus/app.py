"""FastAPI app: routes + SSE."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import browser
from . import config as cfg
from . import report as report_mod
from . import store
from .checks import all_checks  # registers all checks at import time
from .checks._utils import UnsafeTargetURL, validate_target_url
from .checks.base import Env
from .runner import Event, manager

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    manager.start()
    try:
        yield
    finally:
        await browser.shutdown()


app = FastAPI(title="Cerberus", lifespan=lifespan)


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    config = cfg.load()
    return {
        "default_url": config.default_url,
        "known_hosts": list(config.hosts.keys()),
        "gsc_configured": bool(config.gsc_credentials_path and Path(config.gsc_credentials_path).exists()),
        "environments": [e.value for e in Env],
        "default_environment": Env.PRODUCTION.value,
    }


@app.get("/api/checks")
async def api_checks() -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "section": s.section,
            "severity": s.severity.value,
            "title": s.title,
            "default_estimate_ms": s.default_estimate_ms,
        }
        for s in all_checks()
    ]


@app.post("/api/runs")
async def api_create_run(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    try:
        await validate_target_url(url)
    except UnsafeTargetURL as exc:
        raise HTTPException(400, f"refusing to scan internal/private target: {exc}") from exc
    env_raw = (body.get("environment") or "production").strip().lower()
    try:
        environment = Env(env_raw)
    except ValueError:
        valid = ", ".join(e.value for e in Env)
        raise HTTPException(400, f"environment must be one of: {valid}")
    run_id = await manager.enqueue(url, environment)
    return {"run_id": run_id}


@app.get("/api/runs")
async def api_list_runs() -> list[dict[str, Any]]:
    return await asyncio.to_thread(store.list_runs)


@app.get("/api/runs/{run_id}")
async def api_get_run(run_id: str) -> dict[str, Any]:
    run = await asyncio.to_thread(store.get_run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    checks = await asyncio.to_thread(store.get_check_results, run_id)
    toggles = await asyncio.to_thread(store.get_manual_toggles, run_id)
    return {"run": run, "checks": checks, "manual_toggles": toggles}


@app.get("/api/runs/{run_id}/screenshots/{name}.png")
async def api_run_screenshot(run_id: str, name: str) -> FileResponse:
    from . import screenshots as _ss
    # Validate path components BEFORE any filesystem access — '..' or '/' would let an attacker
    # traverse to files outside data/screenshots/. We resolve the final path and confirm it's
    # still under SCREENSHOT_DIR as a belt-and-suspenders check.
    if not run_id or not name:
        raise HTTPException(404, "screenshot not found")
    if any(c in run_id for c in ("..", "/", "\\", "\x00")) or any(c in name for c in ("..", "/", "\\", "\x00")):
        raise HTTPException(404, "screenshot not found")
    path = (_ss.SCREENSHOT_DIR / run_id / f"{name}.png").resolve()
    try:
        path.relative_to(_ss.SCREENSHOT_DIR.resolve())
    except ValueError:
        raise HTTPException(404, "screenshot not found")
    if not path.exists():
        raise HTTPException(404, "screenshot not found")
    return FileResponse(path, media_type="image/png")


@app.get("/api/runs/{run_id}/report.md")
async def api_run_report_markdown(run_id: str) -> PlainTextResponse:
    run = await asyncio.to_thread(store.get_run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    checks = await asyncio.to_thread(store.get_check_results, run_id)
    toggles = await asyncio.to_thread(store.get_manual_toggles, run_id)
    md = report_mod.render_markdown(run, checks, toggles)
    filename = f"cerberus-report-{run_id}.md"
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/runs/{run_id}/manual/{check_id}")
async def api_set_manual(run_id: str, check_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    # Verify the run exists first so a bad run_id returns 404 rather than triggering a SQLite
    # foreign-key 500 (the manual_toggles table FK-references runs.id).
    run = await asyncio.to_thread(store.get_run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    status = (body.get("status") or "").strip()
    if status not in ("Pass", "Fail"):
        await asyncio.to_thread(store.clear_manual_toggle, run_id, check_id)
        return {"cleared": True}
    await asyncio.to_thread(store.set_manual_toggle, run_id, check_id, status)
    return {"ok": True, "marked_status": status}


@app.get("/api/runs/{run_id}/events")
async def api_run_events(request: Request, run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")

    channel = manager.channel(run_id)
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=512)

    async def event_generator():
        # Subscribe FIRST so any events fired between snapshot capture and live stream are queued.
        async with channel.lock:
            channel.subscribers.add(queue)
        try:
            replay_run = await asyncio.to_thread(store.get_run, run_id)
            replay = {
                "run": replay_run,
                "checks": await asyncio.to_thread(store.get_check_results, run_id),
                "manual_toggles": await asyncio.to_thread(store.get_manual_toggles, run_id),
            }
            yield {"event": "snapshot", "data": json.dumps(replay)}
            # If the run is already terminal — and the channel may have been evicted, leaving us
            # with a fresh empty channel that will never see another publish — emit a synthetic
            # run_completed + done so the client knows there's nothing more coming.
            if replay_run and replay_run["status"] in ("completed", "failed", "cancelled"):
                yield {"event": "run_completed", "data": json.dumps({"summary": replay_run.get("summary") or {}})}
                yield {"event": "done", "data": "{}"}
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": ev.type, "data": json.dumps(ev.data)}
                if ev.type == "done":
                    return
        finally:
            async with channel.lock:
                channel.subscribers.discard(queue)

    return EventSourceResponse(event_generator())


# --- Static frontend ---

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
