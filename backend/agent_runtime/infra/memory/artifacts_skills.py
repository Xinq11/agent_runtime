"""In-memory tool output spill, workspace files, and versioned skills mixin."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import time
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from agent_runtime.framework.core.contracts import (
    AuthScope,
    SkillSummary,
    ToolOutputBlob,
    WorkspaceFileRecord,
)


class MemoryArtifactsSkillsMixin:
    """Tool output blobs, cloud workspace files, and versioned skills sharing `self._lock`."""

    # --- Tool Outputs ---
    def save_tool_output(self, ctx, tool_name, full_text, preview, ttl_sec=3600) -> ToolOutputBlob:
        blob = ToolOutputBlob(
            uuid4().hex,
            ctx.auth_scope.tenant_id,
            ctx.auth_scope.user_id,
            tool_name,
            full_text,
            preview,
            len(full_text),
            time.time() + ttl_sec,
        )
        with self._lock:
            self._outputs[blob.ref_id] = blob
        return deepcopy(blob)

    def read_tool_output_page(
        self, ctx, ref_id, offset=0, limit=1200
    ) -> Tuple[Optional[str], Optional[str]]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            return None, "invalid_page"
        with self._lock:
            blob = self._outputs.get(ref_id)
            if not blob or blob.tenant_id != ctx.auth_scope.tenant_id or blob.resource_owner != ctx.auth_scope.user_id:
                return None, "output_not_found"
            if blob.expires_at <= time.time():
                return None, "output_expired"
            return blob.full_text[offset : offset + min(limit, 1200)], None

    # --- Workspace Files ---
    def write_workspace_file(
        self,
        tenant_id: str,
        thread_id: str,
        logical_path: str,
        content: str,
        *,
        actor_id: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> WorkspaceFileRecord:
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        key = (tenant_id, thread_id, logical_path)
        with self._lock:
            prev = self._workspace_files.get(key)
            ver = (prev[0].version + 1) if prev else 1
            rec = WorkspaceFileRecord(
                tenant_id=tenant_id,
                thread_id=thread_id,
                logical_path=logical_path,
                version=ver,
                oss_key=f"workspaces/{tenant_id}/{thread_id}/v{ver}_{uuid4().hex[:8]}/{logical_path}",
                sha256=digest,
                size_bytes=len(raw),
                content_type=content_type,
                updated_by=actor_id,
            )
            self._workspace_files[key] = (rec, content)
            return deepcopy(rec)

    def read_workspace_file(
        self, tenant_id: str, thread_id: str, logical_path: str
    ) -> Tuple[Optional[str], Optional[WorkspaceFileRecord], Optional[str]]:
        with self._lock:
            entry = self._workspace_files.get((tenant_id, thread_id, logical_path))
            if not entry:
                return None, None, "file_not_found"
            return entry[1], deepcopy(entry[0]), None

    def list_workspace_files(self, tenant_id: str, thread_id: str) -> List[WorkspaceFileRecord]:
        with self._lock:
            out = [
                deepcopy(rec)
                for (t, th, _), (rec, _) in self._workspace_files.items()
                if t == tenant_id and th == thread_id
            ]
            return sorted(out, key=lambda r: r.logical_path)

    # --- Versioned Skills ---
    def publish_skill(
        self,
        *,
        scope: AuthScope,
        slug: str,
        description: str,
        content: str,
        acl_roles: List[str],
        tags: Optional[List[str]] = None,
        resources: Optional[Dict[str, str]] = None,
    ) -> SkillSummary:
        key = (scope.tenant_id, slug)
        res_map = dict(resources or {})
        with self._lock:
            entry = self._skills.setdefault(
                key, {"active": None, "revision": 0, "acl": tuple(acl_roles), "status": "active", "versions": {}}
            )
            entry["revision"] += 1
            entry["acl"] = tuple(acl_roles)
            entry["status"] = "active"
            ver_num = entry["revision"]
            vid = f"skv_{uuid4().hex[:12]}"
            summary = SkillSummary(
                tenant_id=scope.tenant_id,
                slug=slug,
                version_id=vid,
                version_num=ver_num,
                description=description,
                tags=tuple(tags or ()),
                acl_roles=tuple(acl_roles),
                resources=tuple(sorted(res_map.keys())),
                revision=ver_num,
                status="active",
            )
            entry["versions"][vid] = (summary, content, res_map)
            entry["active"] = vid
            return deepcopy(summary)

    def list_skills(
        self, scope: AuthScope, *, query: str = "", include_disabled: bool = False
    ) -> List[SkillSummary]:
        roles = set(scope.roles)
        q = query.strip().lower()
        with self._lock:
            out: List[SkillSummary] = []
            for (t, _), entry in self._skills.items():
                if t != scope.tenant_id or not entry["active"]:
                    continue
                if not include_disabled and entry["status"] != "active":
                    continue
                if roles and not roles.intersection(entry["acl"]):
                    continue
                summary = deepcopy(entry["versions"][entry["active"]][0])
                summary.status = entry["status"]
                if q and q not in f"{summary.slug} {summary.description} {' '.join(summary.tags)}".lower():
                    continue
                out.append(summary)
            return sorted(out, key=lambda s: (s.status != "active", s.slug))

    def get_skill_content(
        self,
        scope: AuthScope,
        slug: str,
        *,
        resource: Optional[str] = None,
        include_disabled: bool = False,
    ) -> Tuple[Optional[str], Optional[SkillSummary], Optional[str]]:
        roles = set(scope.roles)
        with self._lock:
            entry = self._skills.get((scope.tenant_id, slug))
            if not entry or not entry["active"] or (not include_disabled and entry["status"] != "active"):
                return None, None, "skill_not_found"
            if roles and not roles.intersection(entry["acl"]):
                return None, None, "skill_not_found"
            summary, body, res_map = entry["versions"][entry["active"]]
            summary = deepcopy(summary)
            summary.status = entry["status"]
            if resource:
                if resource not in res_map:
                    return None, summary, "resource_not_found"
                return res_map[resource], summary, None
            return body, summary, None

    def set_skill_status(self, scope: AuthScope, slug: str, status: str) -> bool:
        if status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        with self._lock:
            entry = self._skills.get((scope.tenant_id, slug))
            if not entry:
                return False
            entry["status"] = status
            return True

    def disable_skill(self, scope: AuthScope, slug: str) -> bool:
        return self.set_skill_status(scope, slug, "disabled")
