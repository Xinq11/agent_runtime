"""Cloud-native Skills management routes backed by PG + OSS (JSON + multipart file upload)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from agent_runtime.api.deps import (
    caller_scope,
    caller_scope_async,
    require_access,
    require_access_async,
    resolve_filter,
)
from agent_runtime.api.schemas import SkillUploadRequest, StatusUpdateRequest
from agent_runtime.extensions.skills.extension import validate_resource_name, validate_skill_slug
from agent_runtime.framework.core.config import MAX_DOCUMENT
from agent_runtime.framework.core.security import ResourceRef

_ALLOWED_SKILL_EXTS = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml"}

router = APIRouter(tags=["Skills Management"])


@router.get("/skills", summary="列出当前可用的全部 Skill")
def list_skills(req: Request, query: str = ""):
    scope = caller_scope(req)
    flt = resolve_filter(req, scope, "skill:read", "skill")
    items = req.app.state.store.list_skills(scope, query=query, access_filter=flt)
    return {"skills": [asdict(s) for s in items]}


@router.get("/skills/{slug}", summary="读取指定 Skill 的 SKILL.md 正文或附属声明式资源")
def get_skill(slug: str, req: Request, resource: str = "", include_disabled: bool = False):
    scope = caller_scope(req)
    clean = slug.strip().lower()
    if include_disabled:
        require_access(req, scope, "skill:manage", ResourceRef("skill", id=clean, tenant_id=scope.tenant_id))
        flt = resolve_filter(req, scope, "skill:manage", "skill")
    else:
        flt = resolve_filter(req, scope, "skill:read", "skill")
    content, meta, err = req.app.state.store.get_skill_content(
        scope,
        clean,
        resource=resource.strip() or None,
        access_filter=flt,
    )
    if err or content is None or meta is None:
        raise HTTPException(404, err or "skill_not_found")
    return {"skill": asdict(meta), "resource": resource or "SKILL.md", "content": content}


def _validate_uploaded_filename(filename: str) -> None:
    try:
        validate_resource_name(filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    ext = PurePosixPath(filename).suffix.lower()
    if ext and ext not in _ALLOWED_SKILL_EXTS:
        raise HTTPException(422, f"不支持的文件类型 '{ext}'，仅允许声明式文本文件（如 .md / .txt）")


@router.post("/skills/upload", summary="上传/发布新版本声明式 Skill 到 OSS + PG（支持文件或 JSON）")
async def upload_skill(req: Request):
    scope = await caller_scope_async(req)
    await require_access_async(req, scope, "skill:manage", ResourceRef("skill", tenant_id=scope.tenant_id))
    raw_resources: dict[str, str] = {}
    tags: list[str] = []
    acl_roles: list[str] | None = None

    if req.headers.get("content-type", "").startswith("multipart/form-data"):
        async with req.form() as form:
            file = form.get("file")
            if isinstance(file, UploadFile):
                if file.filename:
                    _validate_uploaded_filename(file.filename)
                raw = await file.read(MAX_DOCUMENT + 1)
                if len(raw) > MAX_DOCUMENT:
                    raise HTTPException(413, "Skill 文件超过 2 MiB 上限")
                try:
                    content = raw.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise HTTPException(422, "Skill 文件需要 UTF-8 编码") from exc
                fallback_slug = PurePosixPath(file.filename or "").stem
            else:
                content = str(form.get("content") or "")
                fallback_slug = ""
            raw_slug = str(form.get("slug") or fallback_slug)
            description = str(form.get("description") or "")
            acl_roles = [str(r) for r in form.getlist("acl_roles")]
    else:
        try:
            body = SkillUploadRequest.model_validate(await req.json())
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from exc
        raw_slug = body.slug
        description = body.description
        content = body.content
        acl_roles = body.acl_roles
        tags = [t.strip() for t in body.tags if t.strip()]
        raw_resources = body.resources or {}

    try:
        slug = validate_skill_slug(raw_slug)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if not description.strip() or not content.strip():
        raise HTTPException(422, "description 和 Skill 正文不能为空")
    if len(content.encode("utf-8")) > MAX_DOCUMENT:
        raise HTTPException(413, "Skill 正文超过 2 MiB 上限")

    clean_resources: dict[str, str] = {}
    for rname, rbody in raw_resources.items():
        try:
            safe_name = validate_resource_name(rname)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if len(str(rbody).encode("utf-8")) > MAX_DOCUMENT:
            raise HTTPException(413, f"附属资源 {safe_name} 超过 2 MiB 上限")
        clean_resources[safe_name] = str(rbody)

    roles = [str(r).strip() for r in (acl_roles or []) if str(r).strip()]

    summary = await asyncio.to_thread(
        req.app.state.store.publish_skill,
        scope=scope,
        slug=slug,
        description=description.strip(),
        content=content,
        acl_roles=roles,
        tags=tags,
        resources=clean_resources,
    )
    req.app.state.obs.log_event(
        "skill_published",
        "ops",
        "-",
        slug=summary.slug,
        version=summary.version_num,
        msg=f"Published skill {summary.slug} v{summary.version_num}",
    )
    return {"ok": True, "skill": asdict(summary)}


@router.patch("/skills/{slug}/status", summary="切换 Skill 生效/下架状态")
def update_skill_status(slug: str, req: Request, body: StatusUpdateRequest):
    scope = caller_scope(req)
    clean = slug.strip().lower()
    require_access(req, scope, "skill:manage", ResourceRef("skill", id=clean, tenant_id=scope.tenant_id))
    ok = req.app.state.store.set_skill_status(scope, clean, body.status)
    if not ok:
        raise HTTPException(404, "skill_not_found")
    return {"ok": True, "slug": clean, "status": body.status}


@router.delete("/skills/{slug}", summary="下线/禁用指定 Skill")
def disable_skill(slug: str, req: Request):
    scope = caller_scope(req)
    clean = slug.strip().lower()
    require_access(req, scope, "skill:manage", ResourceRef("skill", id=clean, tenant_id=scope.tenant_id))
    ok = req.app.state.store.disable_skill(scope, clean)
    if not ok:
        raise HTTPException(404, "skill_not_found")
    return {"ok": True, "slug": clean, "status": "disabled"}
