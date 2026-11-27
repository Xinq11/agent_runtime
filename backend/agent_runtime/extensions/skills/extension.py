"""Cloud-native Skills extension backed by PG + OSS (`list_skills` & `get_skill` tools).

Zero local filesystem dependency:
  - Skills live in PG (`agent_skills` + immutable `agent_skill_versions`) and OSS (`skills/...`).
  - Two-layer Progressive Disclosure (Claude Code / Codex pattern):
      * Layer 1 (transient `extra_context` in `before_model`): when the caller has <= `inline_limit`
        authorized skills and can invoke `get_skill`, inject a deterministic sorted `[AVAILABLE_SKILLS]`
        metadata catalog so the model can directly call `get_skill(slug)` on step 1 while preserving
        Prefix Cache stability and per-request ACL isolation.
      * Layer 2 (`list_skills` & `get_skill`): bilingual token-overlap search (with clear non-misleading
        fallback when query misses) and on-demand full `SKILL.md` / resource loading from OSS.
  - Executable code uploads (.py/.sh/.exe) are strictly rejected at upload boundary; executable
    customer capabilities must go through reviewed Python `AgentPlugin` packages.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional, Sequence, Tuple

from agent_runtime.framework.core.contracts import ExecutionContext, RiskLevel, SkillSummary, ToolResult
from agent_runtime.framework.core.contracts.skill_resources import validate_resource_name, validate_skill_slug
from agent_runtime.framework.core.protocols import AgentMiddleware, AgentPlugin
from agent_runtime.framework.core.security import (
    AccessControlError,
    AuthorizationError,
    ResourceRef,
    enforce_check,
    enforce_filter,
)
from agent_runtime.framework.tools.gateway import ToolSpec
from agent_runtime.infra.pg.skills import filter_skills_by_query


def _serialize_skills(items: Sequence[SkillSummary]) -> str:
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


class _SkillCatalogMiddleware(AgentMiddleware):
    """ACL-aware transient Layer-1 Skill catalog injection for `before_model`."""

    name = "skills_catalog"

    def __init__(
        self,
        get_deps: Callable[[], Tuple[Any, Any]],
        *,
        inline_limit: int = 20,
        max_inline_chars: int = 1600,
    ) -> None:
        self._get_deps = get_deps
        self._inline_limit = max(0, int(inline_limit))
        self._max_inline_chars = max(256, int(max_inline_chars))

    async def extra_context(self, latest_user_text: str, ctx: ExecutionContext) -> Optional[str]:
        if self._inline_limit == 0:
            return None
        store, access_controller = self._get_deps()
        if store is None or not hasattr(store, "list_skills"):
            return None

        # Never leak Skill catalog into scopes/subagents that are not allowed to invoke `get_skill`
        allowed = ctx.auth_scope.allowed_tools
        if allowed is not None and "get_skill" not in allowed:
            return None

        flt = None
        if access_controller is not None:
            try:
                await enforce_check(
                    access_controller,
                    ctx.auth_scope,
                    "tool:list",
                    ResourceRef(
                        kind="tool",
                        id="get_skill",
                        tenant_id=ctx.auth_scope.tenant_id,
                        attrs={"risk_level": RiskLevel.READ.value, "is_subagent": False},
                    ),
                )
            except AuthorizationError:
                return None
            # `AccessControlError` propagates fail-closed; never swallow controller outages
            flt = await enforce_filter(access_controller, ctx.auth_scope, "skill:read", "skill")

        items: Sequence[SkillSummary] = await asyncio.to_thread(
            store.list_skills, ctx.auth_scope, query="", access_filter=flt
        )
        if not items:
            return None

        # Deterministic sort by slug (no timestamps or query-dependent strings -> Prefix Cache friendly)
        ordered = sorted(items, key=lambda s: s.slug)
        if len(ordered) > self._inline_limit:
            return (
                "[AVAILABLE_SKILLS]\n"
                f"当前权限下共有 {len(ordered)} 个可用平台技能，涉及专业 SOP 或报告规范时，"
                "请先调用 `list_skills` 检索，再按需调用 `get_skill(slug)` 读取完整正文。"
            )

        lines = [
            "[AVAILABLE_SKILLS]",
            "以下为当前权限下可直接按需读取的平台技能（调用 `get_skill(slug)` 读取完整 SOP）：",
        ]
        used = sum(len(line) + 1 for line in lines)
        shown = 0
        for s in ordered:
            desc = " ".join((s.description or "").split())[:140]
            entry = f"- {s.slug}: {desc}" if desc else f"- {s.slug}"
            if shown > 0 and used + len(entry) + 1 > self._max_inline_chars:
                remaining = len(ordered) - shown
                lines.append(f"- ...（其余 {remaining} 个技能可通过 `list_skills` 查看）")
                break
            lines.append(entry)
            used += len(entry) + 1
            shown += 1
        return "\n".join(lines)


class SkillsExtension(AgentPlugin):
    """Registers read-only `list_skills` & `get_skill` tools plus Layer-1 catalog middleware."""

    name = "skills"
    system_prompt = (
        "当遇到特定领域的标准作业流程（SOP）、报告模板或复杂业务规范时，"
        "可优先参考 `[AVAILABLE_SKILLS]` 目录直接调用 `get_skill(slug)` 读取正文严格按步骤执行，"
        "或调用 `list_skills`（query 留空可列全量）检索可用技能。"
    )

    def __init__(self, *, inline_limit: int = 20, max_inline_chars: int = 1600) -> None:
        self.inline_limit = inline_limit
        self.max_inline_chars = max_inline_chars
        self._store: Any = None
        self._access_controller: Any = None

    def middlewares(self) -> Sequence[AgentMiddleware]:
        return (
            _SkillCatalogMiddleware(
                lambda: (self._store, self._access_controller),
                inline_limit=self.inline_limit,
                max_inline_chars=self.max_inline_chars,
            ),
        )

    def register_tools(self, *, registry: Any, store: Any, model: Any, gateway: Any, obs: Any) -> None:
        access_controller = getattr(gateway, "access_controller", None)
        self._store = store
        self._access_controller = access_controller

        async def _list_skills(args: dict, ctx: ExecutionContext) -> Any:
            query = str(args.get("query") or "").strip()
            flt = None
            if access_controller is not None:
                try:
                    flt = await enforce_filter(access_controller, ctx.auth_scope, "skill:read", "skill")
                except AccessControlError as exc:
                    return ToolResult("", "list_skills", f"[acl_error] {exc}", ok=False, error_code="acl_error")
            all_items = store.list_skills(ctx.auth_scope, query="", access_filter=flt)
            if not all_items:
                return "当前权限下暂无可用 Skill。"

            matched = filter_skills_by_query(all_items, query) if query else all_items
            if query and not matched:
                if len(all_items) <= self.inline_limit:
                    return (
                        f"未精确匹配「{query}」，以下为当前权限下全部 {len(all_items)} 个可用 Skill 供参考：\n"
                        + _serialize_skills(all_items)
                    )
                return (
                    f"未找到与「{query}」匹配的 Skill（当前权限下共有 {len(all_items)} 个可用 Skill，"
                    "请尝试更简短关键词或传 query='' 查看目录）。"
                )
            return _serialize_skills(matched)

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
                return ToolResult(
                    "",
                    "get_skill",
                    f"[{err or 'skill_not_found'}] {slug}",
                    ok=False,
                    error_code=err or "skill_not_found",
                )
            header = f"[skill={meta.slug} v{meta.version_num}" + (f" resource={resource}]" if resource else "]")
            return f"{header}\n{content}"

        registry.register(
            ToolSpec(
                name="list_skills",
                description="检索当前用户有权访问的平台技能（Skill）目录及简介；query 可留空列出全部，或输入简短关键词/标签。",
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
