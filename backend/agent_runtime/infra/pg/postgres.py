"""Thin core `PgOssStore` facade composing PostgreSQL + MinIO OSS kernel repositories.

Zero RAG, Skills, or Ops coupling — extensions own their own schemas and repositories.
"""

from __future__ import annotations

import os
from typing import Optional

from agent_runtime.infra.oss.minio import MinioObjectStore
from agent_runtime.infra.pg.approvals import PgApprovalsMixin
from agent_runtime.infra.pg.artifacts import PgArtifactsMixin
from agent_runtime.infra.pg.base import PgBase
from agent_runtime.infra.pg.runs import PgRunsMixin
from agent_runtime.infra.pg.sessions import PgSessionsMixin

DEFAULT_PG_DSN = "postgresql://agent:agent_demo@localhost:5432/agent_runtime"


class PgOssStore(
    PgBase,
    PgSessionsMixin,
    PgRunsMixin,
    PgApprovalsMixin,
    PgArtifactsMixin,
):
    """Unified core PostgreSQL + MinIO OSS store facade."""

    def __init__(self, dsn: Optional[str] = None, oss: Optional[MinioObjectStore] = None):
        super().__init__(dsn or os.getenv("PG_DSN", DEFAULT_PG_DSN))
        self.oss = oss or MinioObjectStore()
        self.s3 = self.oss.client


__all__ = ["PgOssStore"]
