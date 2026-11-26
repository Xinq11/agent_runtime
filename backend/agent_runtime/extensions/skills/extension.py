"""Cloud-native Skills extension backed by PG + OSS (`list_skills` & `get_skill` tools).

Zero local filesystem dependency:
  - Skills live in PG (`agent_skills` + immutable `agent_skill_versions`) and OSS (`skills/...`).
  - Platform admins hot-upload declarative Markdown & plain-text attachments via `/api/v1/skills/upload`.
  - Executable code uploads (.py/.sh/.exe) are strictly rejected at upload boundary; executable
    customer capabilities must go through reviewed Python `AgentPlugin` packages.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent_runtime.framework.core.contracts import ExecutionContext, RiskLevel, ToolResult
from agent_runtime.framework.core.protocols import AgentPlugin
from agent_runtime.framework.core.security import AccessControlError, enforce_filter
from agent_runtime.framework.tools.gateway import ToolSpec

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_FORBIDDEN_RESOURCE_EXTS = (".py", ".sh", ".bash", ".zsh", ".exe", ".dll", ".so", ".bin", ".bat", ".ps1")


def validate_skill_slug(slug: str) -> str:
    cleaned = slug.strip().lower()
    if not _SLUG_RE.match(cleaned):
        raise ValueError("skill slug 须为 2~64 位小写字母、数字、连字符或下划线")
    return cleaned


def validate_resource_name(name: str) -> str:
    cleaned = name.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/") or "\x00" in cleaned:
        raise ValueError(f"非法资源路径: {name}")
    low = cleaned.lower()
    if any(low.endswith(ext) for ext in _FORBIDDEN_RESOURCE_EXTS):
        raise ValueError(f"安全限制：Skill 仅支持声明式文本资源，禁止上传可执行文件 ({name})")
    return cleaned


class SkillsExtension(AgentPlugin):
    """Registers read-only `list_skills` and `get_skill` tools backed by PG + OSS."""

    name = "skills"
    system_prompt = (
        "当遇到特定领域的标准作业流程（SOP）、报告模板或复杂业务规范时，"
        "可先调用 `list_skills` 查找可用技能目录，再按需调用 `get_skill(slug)` 读取正文严格按步骤执行。"
    )

    def register_tools(self, *, registry: Any, store: Any, model: Any, gateway: Any, obs: Any) -> None:
        access_controller = getattr(gateway, "access_controller", None)

        async def _list_skills(args: dict, ctx: ExecutionContext) -> Any:
            query = str(args.get("query") or "").strip()
            flt = None
            if access_controller is not None:
                try:
                    flt = await enforce_filter(access_controller, ctx.auth_scope, "skill:read", "skill")
                except AccessControlError as exc:
                    return ToolResult("", "list_skills", f"[acl_error] {exc}", ok=False, error_code="acl_error")
            items = store.list_skills(ctx.auth_scope, query=query, access_filter=flt)
            if not items:
                return "当前权限下暂无可用 Skill。"
            return json.dumps(
                [
                    {
                        "slug": s.slug,
                        "version": s.version_num,
                        "description": s.description,
                        "tags": list(s.tags),
                        "resources": list(s.resources),
                    }
                    for s in items
                ],
                ensure_ascii=False,
            )

        async def _get_skill(args: dict, ctx: ExecutionContext) -> Any:
            slug = str(args.get("slug") or "").strip().lower()
            resource = str(args.get("resource") or "").strip()
            if not slug:
                return ToolResult("", "get_skill", "[invalid_args] slug 不能为空", ok=False, error_code="invalid_args")
            flt = None
            if access_controller is not None:
                try:
                    flt = await enforce_filter(access_controller, ctx.auth_scope, "skill:read", "skill")
                except AccessControlError as exc:
                    return ToolResult("", "get_skill", f"[acl_error] {exc}", ok=False, error_code="acl_error")
            content, meta, err = store.get_skill_content(
                ctx.auth_scope, slug, resource=resource or None, access_filter=flt
            )
            if err or content is None:
                return ToolResult("", "get_skill", f"[{err or 'skill_not_found'}] {slug}", ok=False, error_code=err or "skill_not_found")
            header = f"[skill={meta.slug} v{meta.version_num}" + (f" resource={resource}]" if resource else "]")
            return f"{header}\n{content}"

        registry.register(
            ToolSpec(
                name="list_skills",
                description="检索当前用户有权访问的平台技能（Skill）目录及简介。",
                required_args=(),
                allowed_args=("query",),
                risk_level=RiskLevel.READ,
                handler=_list_skills,
                parameters_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "default": ""}},
                    "required": [],
                    "additionalProperties": False,
                },
            )
        )

        registry.register(
            ToolSpec(
                name="get_skill",
                description="按 slug 从对象存储读取指定技能（SKILL.md）正文或声明式附属参考文件。",
                required_args=("slug",),
                allowed_args=("slug", "resource"),
                risk_level=RiskLevel.READ,
                handler=_get_skill,
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "resource": {"type": "string", "default": ""},
                    },
                    "required": ["slug"],
                    "additionalProperties": False,
                },
            )
        )
