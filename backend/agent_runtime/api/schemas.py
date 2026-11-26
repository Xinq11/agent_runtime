"""Typed Pydantic request/response schemas powering OpenAPI `/docs` and `/api/v1/*`."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    thread_id: Optional[str] = Field(default=None, description="会话 ID；不传则由服务端生成")
    message: Optional[str] = Field(default=None, description="用户输入正文")
    user_text: Optional[str] = Field(default=None, description="message 的兼容别名")
    request_id: Optional[str] = Field(default=None, description="客户端幂等请求 ID")


class AbortRequest(BaseModel):
    run_id: Optional[str] = Field(default=None, description="要中止的 run_id")
    thread_id: Optional[str] = Field(default=None, description="要中止的 thread_id（与 run_id 二选一）")


class ApprovalResumeRequest(BaseModel):
    approved: bool = Field(..., description="true=同意执行，false=拒绝执行")
    thread_id: Optional[str] = Field(default=None, description="可选校验 thread_id")
    approver_id: Optional[str] = Field(default=None, description="审批人 ID")
    reason: str = Field(default="", description="审批备注")


class CreateThreadRequest(BaseModel):
    thread_id: Optional[str] = None
    title: str = "新对话"


class ModelConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    mode: Optional[str] = Field(default=None, description="auto | deepseek | mock")


class SkillUploadRequest(BaseModel):
    slug: str = Field(..., description="租户内唯一 Skill 标识（小写字母/数字/-/_）")
    description: str = Field(..., description="一句话技能简介（用于列表检索与模型发现）")
    content: str = Field(..., description="SKILL.md 声明式 Markdown 正文")
    acl_roles: Optional[List[str]] = Field(default=None, description="允许访问该 Skill 的角色列表")
    tags: List[str] = Field(default_factory=list, description="可选标签")
    resources: Dict[str, str] = Field(
        default_factory=dict,
        description="可选声明式文本附属文件映射（如 {'references/template.md': '...'}），禁止可执行代码文件",
    )


class KnowledgeUploadRequest(BaseModel):
    title: str
    content: str
    section: str = ""
    acl_roles: Optional[List[str]] = None


class StatusUpdateRequest(BaseModel):
    status: Literal["active", "disabled"] = Field(..., description="active=生效，disabled=下架")
