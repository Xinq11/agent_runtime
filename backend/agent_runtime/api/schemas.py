"""Typed Pydantic request/response schemas powering OpenAPI `/docs` and `/api/v1/*`."""

from __future__ import annotations

from typing import Literal, Optional

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
    approver_id: Optional[str] = Field(default=None, description="审批人 ID（兼容字段，服务端始终取已验证调用主体）")
    reason: str = Field(default="", description="审批备注")


class CreateThreadRequest(BaseModel):
    thread_id: Optional[str] = None
    title: str = "新对话"


class ModelConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    mode: Optional[str] = Field(default=None, description="当前支持 deepseek（预留多模型扩展）")


class MessageFeedbackRequest(BaseModel):
    trace_id: str = Field(..., min_length=1, max_length=128, description="对应助手回复的 trace_id")
    rating: Literal["up", "down"] = Field(default="down", description="反馈类型：up=点赞，down=点踩")
    reason: str = Field(default="", max_length=500, description="可选反馈原因")
    msg_id: Optional[int] = Field(default=None, description="可选关联的 session_messages.msg_id")
    answer_excerpt: str = Field(default="", max_length=400, description="可选回复摘要快照")
