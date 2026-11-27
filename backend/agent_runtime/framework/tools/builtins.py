"""Immutable cloud-native built-in tools (`read`, `write`, `read_tool_output`) backed by PG + OSS.

Zero local-disk dependency: every workspace file is stored in OSS with PG version/ACL
metadata so multi-replica stateless backends share the exact same workspace view.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any, Optional

from agent_runtime.framework.core.contracts import ExecutionContext, RiskLevel, ToolResult
from agent_runtime.framework.core.security import (
    AccessControlError,
    AuthorizationError,
    ResourceRef,
    enforce_check,
)
from agent_runtime.framework.tools.gateway import ToolRegistry, ToolSpec

MAX_WORKSPACE_FILE_BYTES = 1024 * 1024  # 1 MiB
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff./()（） -]+$")


def normalize_logical_path(raw: str) -> str:
    """Validate and normalize a logical workspace path, rejecting traversal or absolute paths."""
    if not isinstance(raw, str):
        raise ValueError("path must be a string")
    cleaned = raw.strip().replace("\\", "/")
    if cleaned in ("", "."):
        return "."
    if cleaned.startswith("/") or "\x00" in cleaned or not _SAFE_PATH_RE.match(cleaned):
        raise ValueError("invalid logical path")
    norm = posixpath.normpath(cleaned)
    if norm == ".." or norm.startswith("../") or norm.startswith("/"):
        raise ValueError("path traversal forbidden")
    return norm


def register_immutable_builtin_tools(
    registry: ToolRegistry,
    store: Any,
    *,
    access_controller: Optional[Any] = None,
) -> None:
    """Register the 3 locked built-in tools (`read`, `write`, `read_tool_output`)."""

    async def handle_read(args: dict, ctx: ExecutionContext) -> Any:
        raw_path = args.get("path", ".")
        try:
            path = normalize_logical_path(str(raw_path))
        except ValueError as exc:
            return ToolResult("", "read", f"[invalid_path] {exc}", ok=False, error_code="invalid_path")

        if path == ".":
            files = store.list_workspace_files(ctx.auth_scope.tenant_id, ctx.thread_id)
            if not files:
                return "当前会话工作区暂无文件。"
            return json.dumps(
                [
                    {
                        "path": f.logical_path,
                        "version": f.version,
                        "size_bytes": f.size_bytes,
                        "content_type": f.content_type,
                        "extracted_chars": f.extracted_chars,
                    }
                    for f in files
                ],
                ensure_ascii=False,
            )

        offset = int(args.get("offset", 0) or 0)
        limit = min(int(args.get("limit", 4000) or 4000), 8000)
        if offset < 0 or limit < 1:
            return ToolResult("", "read", "[invalid_args] offset/limit invalid", ok=False, error_code="invalid_args")

        content, meta, err = store.read_workspace_file(ctx.auth_scope.tenant_id, ctx.thread_id, path)
        if err or content is None:
            return ToolResult("", "read", f"[{err or 'file_not_found'}] {path}", ok=False, error_code=err or "file_not_found")
        total = len(content)
        if offset >= total:
            return f"[file={path} v{meta.version} chars={total}..{total}/{total} has_more=false EOF]"
        end = min(offset + limit, total)
        sliced = content[offset:end]
        if end < total:
            header = f"[file={path} v{meta.version} chars={offset}..{end}/{total} has_more=true next_offset={end}]"
        else:
            header = f"[file={path} v{meta.version} chars={offset}..{end}/{total} has_more=false]"
        return f"{header}\n{sliced}"

    async def handle_write(args: dict, ctx: ExecutionContext) -> Any:
        try:
            path = normalize_logical_path(str(args["path"]))
        except ValueError as exc:
            return ToolResult("", "write", f"[invalid_path] {exc}", ok=False, error_code="invalid_path")
        if path == ".":
            return ToolResult("", "write", "[invalid_path] path cannot be directory root", ok=False, error_code="invalid_path")

        content = args["content"]
        if not isinstance(content, str):
            return ToolResult("", "write", "[invalid_args] content must be string", ok=False, error_code="invalid_args")
        if len(content.encode("utf-8")) > MAX_WORKSPACE_FILE_BYTES:
            return ToolResult("", "write", "[file_too_large] max 1 MiB", ok=False, error_code="file_too_large")

        content_type = str(args.get("content_type") or "text/plain; charset=utf-8")
        rec = store.write_workspace_file(
            ctx.auth_scope.tenant_id,
            ctx.thread_id,
            path,
            content,
            actor_id=ctx.auth_scope.user_id,
            content_type=content_type,
        )
        return {
            "path": rec.logical_path,
            "version": rec.version,
            "size_bytes": rec.size_bytes,
            "oss_key": rec.oss_key,
            "sha256": rec.sha256[:16],
        }

    async def handle_read_output(args: dict, ctx: ExecutionContext) -> Any:
        if not isinstance(args["ref_id"], str):
            return ToolResult("", "read_tool_output", "[invalid_args] ref_id", ok=False, error_code="invalid_args")
        if access_controller is not None:
            try:
                await enforce_check(
                    access_controller,
                    ctx.auth_scope,
                    "artifact:read",
                    ResourceRef(
                        kind="artifact",
                        id=args["ref_id"],
                        tenant_id=ctx.auth_scope.tenant_id,
                        owner_id=ctx.auth_scope.user_id,
                    ),
                )
            except AuthorizationError as exc:
                return ToolResult(
                    "", "read_tool_output", f"[permission_denied] {exc.reason}", ok=False, error_code="permission_denied"
                )
            except AccessControlError as exc:
                return ToolResult("", "read_tool_output", f"[acl_error] {exc}", ok=False, error_code="acl_error")
        page, error = store.read_tool_output_page(
            ctx, args["ref_id"], args.get("offset", 0), args.get("limit", 4000)
        )
        if error:
            return ToolResult("", "read_tool_output", f"[{error}]", ok=False, error_code=error)
        return page

    registry.register_builtin(
        ToolSpec(
            name="read",
            description=(
                "读取当前会话工作区文件（支持文本/CSV/JSON/Markdown 及用户上传的 PDF/Word/Excel 附件；"
                "传 path='.' 列出全部工作区文件，支持 offset/limit 分页读取）。"
            ),
            required_args=(),
            allowed_args=("path", "offset", "limit"),
            risk_level=RiskLevel.READ,
            handler=handle_read,
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 4000},
                },
                "required": [],
                "additionalProperties": False,
            },
        )
    )

    registry.register_builtin(
        ToolSpec(
            name="write",
            description="在当前会话工作区写入或更新文件（报告/CSV/Markdown/JSON，自动版本化存入 OSS）。",
            required_args=("path", "content"),
            allowed_args=("path", "content", "content_type"),
            risk_level=RiskLevel.WRITE,
            handler=handle_write,
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "content_type": {"type": "string", "default": "text/plain; charset=utf-8"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )
    )

    registry.register_builtin(
        ToolSpec(
            name="read_tool_output",
            description=(
                "按字符偏移分页读取本人有权访问的超限落盘工具结果（ref_id）；"
                "单页默认 4000 字符、上限 8000 字符，返回头包含 chars/has_more/next_offset。"
            ),
            required_args=("ref_id",),
            allowed_args=("ref_id", "offset", "limit"),
            risk_level=RiskLevel.READ,
            handler=handle_read_output,
            bypass_truncation=True,
            parameters_schema={
                "type": "object",
                "properties": {
                    "ref_id": {"type": "string"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 4000},
                },
                "required": ["ref_id"],
                "additionalProperties": False,
            },
        )
    )
