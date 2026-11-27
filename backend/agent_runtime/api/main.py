"""FastAPI application entrypoint mounting `/api/v1/*` (canonical) and `/api/*` (compat).

Pure SDK default:
  - `AGENT_EXTENSIONS` defaults to empty (`""`) — zero RAG or Skills tables/routes mounted.
  - `AGENT_ENABLE_OPS` defaults to `"0"` — reference admin `/ops` console is opt-in.
Reference demos (e.g. `docker-compose.yml`) explicitly enable extensions & ops routes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import os
import time
from typing import Optional, Sequence

from fastapi import APIRouter, FastAPI, Request
import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_runtime import __version__ as SDK_VERSION
from agent_runtime.api import approvals, chat_stream, state, system, threads
from agent_runtime.framework.agent.app import AgentRuntimeApp, load_extension
from agent_runtime.framework.core.protocols import AgentPlugin
from agent_runtime.framework.core.security import load_access_controller, load_identity_resolver
from agent_runtime.infra.llm.provider import OpenAICompatibleProvider
from agent_runtime.infra.pg.postgres import PgOssStore
from agent_runtime.infra.plg.telemetry import Telemetry

DEFAULT_EXTENSIONS = ""


def _load_configured_extensions() -> list[AgentPlugin]:
    raw = os.getenv("AGENT_EXTENSIONS", os.getenv("AGENT_PLUGIN", DEFAULT_EXTENSIONS)).strip()
    if not raw or raw.lower() in ("none", "off"):
        return []
    return [load_extension(item.strip()) for item in raw.split(",") if item.strip()]


def _ops_enabled() -> bool:
    return os.getenv("AGENT_ENABLE_OPS", "0").strip().lower() in ("1", "true", "yes", "on")


def create_app(
    *,
    extensions: Optional[Sequence[AgentPlugin]] = None,
    enable_ops: Optional[bool] = None,
) -> FastAPI:
    configured_extensions = list(extensions) if extensions is not None else _load_configured_extensions()
    mount_ops = _ops_enabled() if enable_ops is None else bool(enable_ops)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        dsn = os.environ["PG_DSN"]
        store = PgOssStore(dsn)
        await asyncio.to_thread(store.open_pool)
        try:
            await asyncio.to_thread(store.bootstrap)

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
                    extensions=configured_extensions,
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
                    extensions=[e.name for e in configured_extensions],
                )
                worker = asyncio.create_task(obs.push())
                try:
                    yield
                finally:
                    await runtime.compactor.drain()
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
    for sub in (system.router, threads.router, chat_stream.router, approvals.router, state.router):
        v1_router.include_router(sub)
    for ext in configured_extensions:
        for ext_router in ext.routers():
            v1_router.include_router(ext_router)
    if mount_ops:
        from agent_runtime.api import ops

        v1_router.include_router(ops.router)

    app.include_router(v1_router, prefix="/api/v1")
    app.include_router(v1_router, prefix="/api", include_in_schema=False)
    app.include_router(system.metrics_router)
    return app


app = create_app()
