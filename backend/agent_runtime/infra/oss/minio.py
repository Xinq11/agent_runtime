"""MinIO / S3-compatible object storage adapter for `agent_runtime.infra.oss`."""

from __future__ import annotations

import os
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from agent_runtime.framework.core.config import BUCKET


class MinioObjectStore:
    """Thin S3v4 wrapper for immutable tool outputs, workspace files, knowledge docs, and skills."""

    def __init__(
        self,
        *,
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket: str = BUCKET,
    ) -> None:
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
            aws_access_key_id=access_key or os.getenv("MINIO_ROOT_USER", "minioadmin"),
            aws_secret_access_key=secret_key or os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"),
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=3,
                read_timeout=15,
            ),
        )

    def head_bucket(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def put_text(self, key: str, text: str, *, content_type: str = "text/plain; charset=utf-8") -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType=content_type,
        )

    def get_text(self, key: str) -> str:
        with self.client.get_object(Bucket=self.bucket, Key=key)["Body"] as stream:
            return stream.read().decode("utf-8")
