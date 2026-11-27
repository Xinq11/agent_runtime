"""Thin Skill Harness adapter: wires any customer `SkillProvider` into tools & Layer-1 catalog."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Optional, Sequence

from agent_runtime.framework.core.contracts import ExecutionContext, RiskLevel, ToolResult
from agent_runtime.framework.core.protocols import AgentMiddleware, SkillMeta, SkillProvider
from agent_runtime.framework.core.security import (
    AuthorizationError,
    ResourceRef,
    enforce_check,
)
from agent_runtime.framework.tools.registry import ToolRegistry, ToolSpec


async def _call_provider(fn, *args, **kwargs):
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    val = await asyncio.to_thread(fn, *args, **kwargs)
    return await val if inspect.isawaitable(val) else val


def _serialize_skills(items: Sequence[SkillMeta]) -> str:
    return json.dumps(
        [
            {
                "slug": s.slug,
                "description": s.description,
                "tags": list(s.tags),
                "resources": list(s.resources),
            }
            for s in items
        ],
        ensure_ascii=False,
    )


class SkillCatalogMiddleware(AgentMiddleware):
    """Transient Layer-1 Skill catalog injection (`before_model`) with Prefix Cache stability."""

    name = "skills_catalog"

    def __init__(
        self,
        provider: SkillProvider,
        *,
        access_controller: Optional[Any] = None,
        inline_limit: int = 20,
        max_inline_chars: int = 1600,
    ) -> None:
        self._provider = provider
        self._access_controller = access_controller
        self._inline_limit = max(0, int(inline_limit))
        self._max_inline_chars = max(256, int(max_inline_chars))

    async def extra_context(self, latest_user_text: str, ctx: ExecutionContext) -> Optional[str]:
        if self._inline_limit == 0:
            return None

        # Never leak Skill catalog into scopes/subagents that cannot invoke `get_skill`
        allowed = ctx.auth_scope.allowed_tools
        if allowed is not None and "get_skill" not in allowed:
            return None

        if self._access_controller is not None:
            try:
                await enforce_check(
                    self._access_controller,
                    ctx.auth_scope,
                    "tool:list",
                    ResourceRef(
                        kind="tool",
                        id="get_skill",
                        attrs={"risk_level": RiskLevel.READ.value, "is_subagent": False},
                    ),
                )
            except AuthorizationError:
                return None

        items: Sequence[SkillMeta] = await _call_provider(self._provider.list_skills, ctx.auth_scope, "")
        if not items:
            return None

        # Deterministic sort by slug (no timestamps or query-dependent text -> Prefix Cache friendly)
        ordered = sorted(items, key=lambda s: s.slug)
        if len(ordered) > self._inline_limit:
            return (
                "[AVAILABLE_SKILLS]\n"
                f"当前权限下共有 {len(ordered)} 个可用技能，涉及对应专业 SOP 或模板时，"
                "请先调用 `list_skills` 检索，再按需调用 `get_skill(slug)` 读取正文。"
            )

        lines = [
            "[AVAILABLE_SKILLS]",
            (
                "以下为可选技能目录（仅当用户请求明确属于某项技能的适用领域时，才调用 `get_skill(slug)` 读取正文；"
                "若与当前问题无关，请完全忽略此目录，严禁在回答中提及、解释或推荐无关技能）："
            ),
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


def register_skill_tools(
    registry: ToolRegistry,
    provider: SkillProvider,
    *,
    inline_limit: int = 20,
) -> None:
    """Register standard read-only `list_skills` and `get_skill` tools backed by `provider`."""

    async def _list_skills(args: dict, ctx: ExecutionContext) -> Any:
        query = str(args.get("query") or "").strip()
        matched: Sequence[SkillMeta] = await _call_provider(provider.list_skills, ctx.auth_scope, query)
        if matched:
            return _serialize_skills(matched)
        if query:
            all_items: Sequence[SkillMeta] = await _call_provider(provider.list_skills, ctx.auth_scope, "")
            if not all_items:
                return "当前权限下暂无可用 Skill。"
            if len(all_items) <= inline_limit:
                return (
                    f"未精确匹配「{query}」，以下为当前权限下全部 {len(all_items)} 个可用 Skill 供参考：\n"
                    + _serialize_skills(all_items)
                )
            return (
                f"未找到与「{query}」匹配的 Skill（当前权限下共有 {len(all_items)} 个可用 Skill，"
                "请尝试更简短关键词或传 query='' 查看目录）。"
            )
        return "当前权限下暂无可用 Skill。"

    async def _get_skill(args: dict, ctx: ExecutionContext) -> Any:
        slug = str(args.get("slug") or "").strip().lower()
        resource = str(args.get("resource") or "").strip() or None
        if not slug:
            return ToolResult("", "get_skill", "[invalid_args] slug 不能为空", ok=False, error_code="invalid_args")
        content = await _call_provider(provider.get_skill, ctx.auth_scope, slug, resource)
        if content is None:
            return ToolResult("", "get_skill", f"[skill_not_found] {slug}", ok=False, error_code="skill_not_found")
        header = f"[skill={slug}" + (f" resource={resource}]" if resource else "]")
        return f"{header}\n{content}"

    registry.register(
        ToolSpec(
            name="list_skills",
            description="检索当前用户有权访问的技能（Skill）目录及简介；query 可留空列出全部，或输入简短关键词。",
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
            description="按 slug 读取指定技能的正文（如 SKILL.md）或附属参考文件。",
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
