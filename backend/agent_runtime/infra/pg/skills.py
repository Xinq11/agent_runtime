"""PostgreSQL + OSS repository mixin for versioned declarative Skills."""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Tuple

from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.contracts import AuthScope, SkillSummary
from agent_runtime.framework.core.security import AccessFilter
from agent_runtime.infra.pg.acl_sql import compile_acl_sql, default_access_filter


class PgSkillsMixin:
    """PG skill catalog backed by immutable OSS version prefixes."""

    @staticmethod
    def _row_to_skill(r: dict) -> SkillSummary:
        return SkillSummary(
            tenant_id=r["tenant_id"],
            slug=r["slug"],
            version_id=r["version_id"],
            version_num=r["version_num"],
            description=r["description"],
            tags=tuple(r["tags"] or ()),
            acl_roles=tuple(r["acl_roles"] or ()),
            resources=tuple(r["resources"] or ()),
            revision=r["revision"],
            status=r["status"],
        )

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
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        vid = uid("skv")
        # Unique immutable OSS prefix keyed by version_id so concurrent publishes never collide
        prefix = f"skills/{scope.tenant_id}/{slug}/{vid}"
        main_key = f"{prefix}/SKILL.md"
        self.oss.put_text(main_key, content, content_type="text/markdown; charset=utf-8")
        res_names = sorted((resources or {}).keys())
        for rname in res_names:
            self.oss.put_text(f"{prefix}/{rname}", (resources or {})[rname])

        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO agent_skills(tenant_id,slug,active_version_id,revision,acl_roles,status,updated_at)
                   VALUES (%s,%s,NULL,0,%s,'active',now()) ON CONFLICT (tenant_id,slug) DO NOTHING""",
                (scope.tenant_id, slug, list(acl_roles)),
            )
            cur.execute(
                "SELECT revision FROM agent_skills WHERE tenant_id=%s AND slug=%s FOR UPDATE",
                (scope.tenant_id, slug),
            )
            next_ver = int(cur.fetchone()[0]) + 1
            cur.execute(
                """INSERT INTO agent_skill_versions
                   (version_id,tenant_id,slug,version_num,description,tags,manifest,oss_key,sha256,resources,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s)""",
                (
                    vid,
                    scope.tenant_id,
                    slug,
                    next_ver,
                    description,
                    list(tags or []),
                    json.dumps({"slug": slug, "version": next_ver, "oss_prefix": prefix}),
                    main_key,
                    digest,
                    json.dumps(res_names),
                    scope.user_id,
                ),
            )
            cur.execute(
                """UPDATE agent_skills
                   SET active_version_id=%s, revision=%s, acl_roles=%s, status='active', updated_at=now()
                   WHERE tenant_id=%s AND slug=%s""",
                (vid, next_ver, list(acl_roles), scope.tenant_id, slug),
            )
        return SkillSummary(
            tenant_id=scope.tenant_id,
            slug=slug,
            version_id=vid,
            version_num=next_ver,
            description=description,
            tags=tuple(tags or ()),
            acl_roles=tuple(acl_roles),
            resources=tuple(res_names),
            revision=next_ver,
            status="active",
        )

    def list_skills(
        self,
        scope: AuthScope,
        *,
        query: str = "",
        include_disabled: bool = False,
        bypass_acl: bool = False,
        access_filter: Optional[AccessFilter] = None,
    ) -> List[SkillSummary]:
        flt = access_filter or default_access_filter(
            scope, unrestricted=bypass_acl, include_disabled=include_disabled
        )
        acl_sql, acl_params = compile_acl_sql(flt, scope=scope, alias="s")
        rows = self.rows(
            f"""SELECT s.tenant_id, s.slug, v.version_id, v.version_num, v.description,
                       v.tags, s.acl_roles, v.resources, s.revision, s.status
                FROM agent_skills s
                JOIN agent_skill_versions v ON v.version_id = s.active_version_id
                WHERE {acl_sql}
                ORDER BY (s.status = 'active') DESC, s.slug""",
            acl_params,
        )
        q = query.strip().lower()
        out: List[SkillSummary] = []
        for r in rows:
            summary = self._row_to_skill(r)
            if q and q not in f"{summary.slug} {summary.description} {' '.join(summary.tags)}".lower():
                continue
            out.append(summary)
        return out

    def get_skill_content(
        self,
        scope: AuthScope,
        slug: str,
        *,
        resource: Optional[str] = None,
        include_disabled: bool = False,
        bypass_acl: bool = False,
        access_filter: Optional[AccessFilter] = None,
    ) -> Tuple[Optional[str], Optional[SkillSummary], Optional[str]]:
        flt = access_filter or default_access_filter(
            scope, unrestricted=bypass_acl, include_disabled=include_disabled
        )
        acl_sql, acl_params = compile_acl_sql(flt, scope=scope, alias="s")
        rows = self.rows(
            f"""SELECT s.tenant_id, s.slug, v.version_id, v.version_num, v.description,
                       v.tags, s.acl_roles, v.resources, s.revision, s.status, v.oss_key, v.manifest
                FROM agent_skills s
                JOIN agent_skill_versions v ON v.version_id = s.active_version_id
                WHERE {acl_sql} AND s.slug=%(slug)s""",
            {**acl_params, "slug": slug},
        )
        if not rows:
            return None, None, "skill_not_found"
        r = rows[0]
        summary = self._row_to_skill(r)
        if resource:
            if resource not in summary.resources:
                return None, summary, "resource_not_found"
            prefix = (r["manifest"] or {}).get("oss_prefix") or r["oss_key"].rsplit("/", 1)[0]
            return self.oss.get_text(f"{prefix}/{resource}"), summary, None
        return self.oss.get_text(r["oss_key"]), summary, None

    def set_skill_status(self, scope: AuthScope, slug: str, status: str) -> bool:
        if status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        with self.transaction() as cur:
            cur.execute(
                "UPDATE agent_skills SET status=%s, updated_at=now() "
                "WHERE tenant_id=%s AND slug=%s RETURNING slug",
                (status, scope.tenant_id, slug),
            )
            return bool(cur.fetchone())

    def disable_skill(self, scope: AuthScope, slug: str) -> bool:
        return self.set_skill_status(scope, slug, "disabled")
