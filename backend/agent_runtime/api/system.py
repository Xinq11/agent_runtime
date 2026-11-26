"""System routes: health probe, Studio/SDK metadata, Prometheus metrics, and model settings."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from agent_runtime import __version__ as SDK_VERSION
from agent_runtime.api.schemas import ModelConfigUpdateRequest
from agent_runtime.framework.core.config import DEFAULT_BASE, resolve_identity
from agent_runtime.framework.core.contracts import PROTOCOL_VERSION

router = APIRouter(tags=["System & Metadata"])


@router.get("/health", summary="服务健康检查（PG + MinIO + LLM 模式）")
def health(req: Request):
    store = req.app.state.store
    store.rows("SELECT 1")
    store.oss.head_bucket()
    return {
        "ok": True,
        "status": "ok",
        "sdk_version": SDK_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "postgres": "ok",
        "minio": "ok",
        **req.app.state.model.config.public(),
    }


@router.get("/meta", summary="获取框架协议版本、已启用扩展、工具清单与 UI 元信息")
def app_meta(req: Request):
    runtime = req.app.state.runtime
    scope = resolve_identity(req, default_scope=runtime.default_identity())
    skills = []
    if hasattr(req.app.state.store, "list_skills"):
        skills = [
            {
                "slug": s.slug,
                "version": s.version_num,
                "description": s.description,
                "tags": list(s.tags),
                "resources": list(s.resources),
            }
            for s in req.app.state.store.list_skills(scope)
        ]
    return {
        "sdk_version": SDK_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "extensions": [e.name for e in runtime.extensions],
        "sse_events": [
            "meta",
            "token",
            "tool_status",
            "subagent_status",
            "approval_required",
            "done",
            "cancelled",
            "error",
        ],
        **runtime.metadata().to_dict(),
        "tools": [
            {
                "name": s.name,
                "description": s.description,
                "risk_level": s.risk_level.value,
                "is_subagent": s.is_subagent,
            }
            for s in runtime.registry.list_specs()
        ],
        "skills": skills,
    }


@router.get("/config/model", summary="查询当前模型 Provider 配置")
async def get_model(req: Request):
    return req.app.state.model.config.public()


@router.post("/config/model", summary="热更新当前进程模型配置")
async def update_model(req: Request, payload: ModelConfigUpdateRequest):
    body = payload.model_dump(exclude_none=True)
    current = req.app.state.model.config
    cfg = replace(current, **{k: str(v).strip() for k, v in body.items()})
    base = urlsplit(cfg.base_url)
    trusted = {urlsplit(DEFAULT_BASE).netloc, "api.deepseek.com", "api.openai.com"}
    if (
        not base.hostname
        or base.netloc not in trusted
        or base.username
        or base.query
        or base.fragment
        or base.scheme != "https"
        and (base.scheme, base.netloc) != (urlsplit(DEFAULT_BASE).scheme, urlsplit(DEFAULT_BASE).netloc)
    ):
        raise HTTPException(422, "请使用受信任的模型服务端点（DeepSeek / OpenAI 或部署时配置的 DEEPSEEK_BASE_URL）")
    if cfg.mode not in {"deepseek", "auto", "mock"} or not cfg.model:
        raise HTTPException(422, "mode 或 model 无效")
    if base.netloc != urlsplit(current.base_url).netloc and "api_key" not in body:
        cfg = replace(cfg, api_key="")
    req.app.state.model.config = replace(cfg, base_url=cfg.base_url.rstrip("/"))
    return req.app.state.model.config.public()


metrics_router = APIRouter(tags=["Observability"])


@metrics_router.get("/metrics", summary="Prometheus 指标暴露端点")
def metrics(req: Request):
    return Response(
        generate_latest(req.app.state.obs.prom),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
