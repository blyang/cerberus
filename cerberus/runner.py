"""Run lifecycle: queue, executor, per-run pub/sub for SSE."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from typing import Any

from . import config as cfg
from . import store
from .checks import all_checks
from .checks.base import (
    CheckContext,
    CheckResult,
    CheckSpec,
    Env,
    Severity,
    Status,
    SubStep,
)
from .eta import EtaTracker

log = logging.getLogger(__name__)

# Tunables.
HTTP_CONCURRENCY = 8
BROWSER_DEPENDENT_IDS = {"A1", "D3", "H1", "H2", "H3"}
LIGHTHOUSE_DEPENDENT_IDS = {"E1", "E2", "E3", "E3a-perf", "E3a-seo", "E3b"}
LIGHTHOUSE_FIXTURE_ESTIMATE_MS = 40_000  # mobile + desktop in parallel

# Hard upper bound per check function. Lighthouse-dependent E checks just await the fixture, so
# they finish quickly once it lands; the fixture itself has its own 180s subprocess timeout.
# H3 runs two parallel headless renders at networkidle (30s each, parallel), so 90s is generous.
PER_CHECK_TIMEOUT_S = 90.0


@dataclasses.dataclass
class Event:
    type: str
    data: dict[str, Any]

    def to_sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data)}\n\n"


class RunChannel:
    """Per-run pub/sub. Subscribers get an asyncio.Queue of events."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[Event]] = set()
        self.completed: bool = False
        self.lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    async def close(self) -> None:
        async with self.lock:
            self.completed = True
            for q in list(self.subscribers):
                try:
                    q.put_nowait(Event("done", {}))
                except asyncio.QueueFull:
                    pass


class RunManager:
    """Singleton orchestrator: enqueue runs, spin up workers, hold pub/sub channels."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.channels: dict[str, RunChannel] = {}
        self.eta = EtaTracker()
        self.worker_task: asyncio.Task[None] | None = None
        self.current_run_id: str | None = None

    def start(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            self.eta.load()
            self.worker_task = asyncio.create_task(self._worker_loop())

    def channel(self, run_id: str) -> RunChannel:
        if run_id not in self.channels:
            self.channels[run_id] = RunChannel()
        return self.channels[run_id]

    async def enqueue(self, url: str, environment: Env = Env.PRODUCTION) -> str:
        run_id = await asyncio.to_thread(store.create_run, url, environment.value)
        # Pre-seed pending check rows so the frontend can render the full list immediately.
        def seed():
            for spec in all_checks():
                store.upsert_check_result(
                    run_id, spec.id, Status.PENDING.value, summary="",
                    details={"title": spec.title, "severity": spec.severity.value},
                )
        await asyncio.to_thread(seed)
        await self.queue.put(run_id)
        return run_id

    async def _worker_loop(self) -> None:
        while True:
            try:
                run_id = await self.queue.get()
            except asyncio.CancelledError:
                return
            self.current_run_id = run_id
            try:
                await self._execute_run(run_id)
            except Exception as exc:  # pragma: no cover
                log.exception("run %s crashed", run_id)
                await asyncio.to_thread(store.set_run_status, run_id, "failed", str(exc))
            finally:
                self.current_run_id = None
                ch = self.channels.get(run_id)
                if ch:
                    await ch.close()
                    # Evict the channel after a grace period so any newly-attaching SSE clients
                    # that join right at run completion still see the 'done' event in their queue.
                    asyncio.create_task(self._evict_channel(run_id, delay=30))

    async def _evict_channel(self, run_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.channels.pop(run_id, None)

    async def _execute_run(self, run_id: str) -> None:
        run = await asyncio.to_thread(store.get_run, run_id)
        if not run:
            return
        url = run["url"]
        site_config = await asyncio.to_thread(cfg.load)
        try:
            env = Env(run.get("environment") or Env.PRODUCTION.value)
        except ValueError:
            env = Env.PRODUCTION
        ctx = CheckContext(url=url, site_config=site_config, run_id=run_id, environment=env)
        ch = self.channel(run_id)
        await asyncio.to_thread(store.set_run_status, run_id, "running")
        await ch.publish(Event("run_started", {"run_id": run_id, "url": url}))

        specs = all_checks()
        completed_ids: set[str] = set()

        # Helper for ETA broadcast.
        async def broadcast_progress() -> None:
            remaining = self.eta.estimate_remaining_ms(specs, completed_ids)
            await ch.publish(
                Event(
                    "progress",
                    {
                        "completed": len(completed_ids),
                        "total": len(specs),
                        "eta_ms": remaining,
                    },
                )
            )

        await broadcast_progress()

        # Phase 1: kick off Lighthouse fixture in background; HTTP-bound checks run in parallel.
        from .lighthouse import FIXTURE_KEY, build_fixture  # local import (lazy)
        lh_task: asyncio.Task[Any] | None = None
        if any(s.id in LIGHTHOUSE_DEPENDENT_IDS for s in specs):
            await ch.publish(
                Event(
                    "fixture_started",
                    {"name": "lighthouse", "estimate_ms": LIGHTHOUSE_FIXTURE_ESTIMATE_MS},
                )
            )
            lh_task = asyncio.create_task(build_fixture(url))
            ctx.cache[FIXTURE_KEY] = lh_task  # checks await this

        sem = asyncio.Semaphore(HTTP_CONCURRENCY)

        async def run_one(spec: CheckSpec) -> None:
            applicable = ctx.environment in spec.applicable_envs
            if not applicable:
                # Skip the Running upsert/event entirely — operator sees N/A from the start.
                applicable_list = sorted(e.value for e in spec.applicable_envs)
                result = CheckResult(
                    status=Status.NA,
                    summary=f"Not meaningful in {ctx.environment.value}; runs in {', '.join(applicable_list)}.",
                    details={"skipped_reason": "environment_mismatch",
                             "applicable_envs": applicable_list,
                             "current_env": ctx.environment.value},
                )
                runtime_ms = 0
            else:
                await ch.publish(Event("check_running", {"check_id": spec.id}))
                await asyncio.to_thread(
                    store.upsert_check_result,
                    run_id, spec.id, Status.RUNNING.value,
                    "", {"title": spec.title, "severity": spec.severity.value},
                    int(time.time() * 1000), None,
                )
                t0 = time.time()
                try:
                    async with sem:
                        result = await asyncio.wait_for(spec.func(ctx), timeout=PER_CHECK_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.warning("check %s timed out after %ss", spec.id, PER_CHECK_TIMEOUT_S)
                    result = CheckResult(
                        status=Status.FAIL,
                        summary=f"Check timed out after {int(PER_CHECK_TIMEOUT_S)}s.",
                        details={"timeout_s": PER_CHECK_TIMEOUT_S},
                    )
                except Exception as exc:
                    log.exception("check %s failed", spec.id)
                    # Don't echo the raw exception text into the persisted details — it can include
                    # filesystem paths, internal URLs, or third-party error bodies that we don't want
                    # exposed via /api/runs/{id} to anyone on the tailnet. Server log has the full trace.
                    result = CheckResult(
                        status=Status.FAIL,
                        summary=f"Check raised an internal error ({type(exc).__name__}); see server logs.",
                        details={"error_type": type(exc).__name__},
                    )
                runtime_ms = int((time.time() - t0) * 1000)
                await asyncio.to_thread(self.eta.record, spec.id, runtime_ms)
            details = dict(result.details)
            details.update({
                "title": spec.title,
                "severity": spec.severity.value,
                "section": spec.section,
                "instruction": result.instruction,
                "sub_steps": [dataclasses.asdict(s) for s in result.sub_steps],
                "runtime_ms": runtime_ms,
            })
            await asyncio.to_thread(
                store.upsert_check_result,
                run_id, spec.id, result.status.value,
                result.summary, details,
                None, int(time.time() * 1000),
            )
            completed_ids.add(spec.id)
            await ch.publish(
                Event(
                    "check_completed",
                    {
                        "check_id": spec.id,
                        "status": result.status.value,
                        "summary": result.summary,
                        "details": details,
                    },
                )
            )
            await broadcast_progress()

        await asyncio.gather(*(run_one(s) for s in specs))

        if lh_task is not None and not lh_task.done():
            await lh_task

        # Compute summary.
        results = await asyncio.to_thread(store.get_check_results, run_id)
        summary = _summarize(results)
        await asyncio.to_thread(store.set_run_summary, run_id, summary)
        await asyncio.to_thread(store.set_run_status, run_id, "completed")
        await ch.publish(Event("run_completed", {"summary": summary}))


def _summarize(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {s.value: 0 for s in Status}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


# Singleton.
manager = RunManager()
