"""FastAPI application entrypoint mounting `/api/v1/*` (canonical) and `/api/*` (compat)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import os
import time

from fastapi import APIRouter, FastAPI, Request
import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_runtime import __version__ as SDK_VERSION
from agent_runtime.api import approvals, chat_stream, ops, skills, state, system, threads
from agent_runtime.framework.agent.app import AgentRuntimeApp, load_extension
from agent_runtime.framework.core.security import load_access_controller, load_identity_resolver
from agent_runtime.infra.llm.provider import OpenAICompatibleProvider
from agent_runtime.infra.pg.postgres import PgOssStore
from agent_runtime.infra.plg.telemetry import Telemetry

DEFAULT_EXTENSIONS = (
    "agent_runtime.extensions.rag.extension:RagExtension,"
    "agent_runtime.extensions.skills.extension:SkillsExtension,"
    "agent_runtime.extensions.customers.enterprise_expense:EnterpriseExpensePlugin"
)


def _load_configured_extensions():
    raw = os.getenv("AGENT_EXTENSIONS", os.getenv("AGENT_PLUGIN", DEFAULT_EXTENSIONS)).strip()
    if not raw or raw.lower() in ("none", "off"):
        return []
    return [load_extension(item.strip()) for item in raw.split(",") if item.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ["PG_DSN"]
    store = PgOssStore(dsn)
    await asyncio.to_thread(store.open_pool)
    try:
        await asyncio.to_thread(store.bootstrap)
        extensions = _load_configured_extensions()

        async with httpx.AsyncClient(follow_redirects=False) as client, AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()
            obs = Telemetry(store, client)
            model = OpenAICompatibleProvider(client, obs)
            access_controller = load_access_controller()
            runtime = AgentRuntimeApp(
                model=model,
                store=store,
                obs=obs,
                checkpointer=saver,
                extensions=extensions,
                access_controller=access_controller,
            )
            await asyncio.to_thread(runtime.bootstrap_extensions)
            identity_resolver = load_identity_resolver(default_scope=runtime.default_identity())

            app.state.runtime = runtime
            app.state.identity_resolver = identity_resolver
            app.state.store = store
            app.state.obs = obs
            app.state.model = model
            app.state.graph = runtime.graph

            obs.log_event(
                "server_started",
                "system",
                "system",
                model=model.config.model,
                mode=model.config.effective,
                extensions=[e.name for e in extensions],
            )
            worker = asyncio.create_task(obs.push())
            try:
                yield
            finally:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(obs.queue.join(), 3)
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
    finally:
        await asyncio.to_thread(store.close_pool)


app = FastAPI(
    title="Agent Runtime SDK API",
    version=SDK_VERSION,
    description=(
        "通用企业级 Agent Runtime 后端协议层（HTTP + SSE v1）。"
        "任何自定义前端（React / Vue / 门户 / 小程序 / CLI）均可通过本接口独立接入。"
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def access_log_middleware(req: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(req)
    path = req.url.path
    skip = ("/api/health", "/api/v1/health", "/metrics", "/api/ops/overview", "/api/v1/ops/overview")
    if path not in skip and hasattr(app.state, "obs"):
        dur_ms = max(1, int((time.monotonic() - t0) * 1000))
        app.state.obs.log_event(
            "http_request",
            "http",
            req.query_params.get("thread_id", "-"),
            method=req.method,
            path=path,
            status_code=response.status_code,
            duration_ms=dur_ms,
        )
    return response


v1_router = APIRouter()
for sub in (system.router, threads.router, chat_stream.router, approvals.router, state.router, skills.router, ops.router):
    v1_router.include_router(sub)

# Canonical v1 endpoints (shown in OpenAPI /docs)
app.include_router(v1_router, prefix="/api/v1")
# Backward-compatible /api aliases (hidden from OpenAPI to keep /docs crisp)
app.include_router(v1_router, prefix="/api", include_in_schema=False)
# Prometheus scrape endpoint at /metrics
app.include_router(system.metrics_router)
