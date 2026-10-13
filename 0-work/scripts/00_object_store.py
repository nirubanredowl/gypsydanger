#!/usr/bin/env python3
"""S3-compatible object storage for Gypsy Danger (AWS S3 or Neon Object Storage).

Object *keys* are backend-neutral and match the layout documented in
0-work/docs/extraction-pipeline.md (formerly s3://gypsy-danger-asx-…/…).
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - optional until pip install boto3
    boto3 = None  # type: ignore[assignment]
    Config = None  # type: ignore[misc, assignment]
    ClientError = Exception  # type: ignore[misc, assignment]


DEFAULT_PREFIXES = (
    "entities/",
    "parsed/",
    "manifests/",
    "catalog/",
    "parse_3a/",
    "data/",
    "scripts/",
    "logs/",
    "cfo_changes/",
)


@dataclass(frozen=True)
class StoreConfig:
    backend: str  # "aws" | "neon"
    bucket: str
    endpoint_url: str | None
    region: str | None

    @property
    def uri_prefix(self) -> str:
        scheme = "neon" if self.backend == "neon" else "s3"
        return f"{scheme}://{self.bucket}/"


def _resolve_backend(explicit: str | None) -> str:
    if explicit:
        return explicit.lower()
    if os.environ.get("AWS_ENDPOINT_URL_S3"):
        return "neon"
    return "aws"


def bucket_from_env() -> str:
    return (
        os.environ.get("GYPSY_STORAGE_BUCKET", "").strip()
        or os.environ.get("GYPSY_S3_BUCKET", "").strip()
    )


def store_for_bucket(bucket: str, *, backend: str | None = None) -> "ObjectStore":
    """Explicit bucket (CLI --bucket) with env-derived backend and credentials."""
    return from_env(backend=backend, bucket=bucket)


def from_env(
    *,
    backend: str | None = None,
    bucket: str | None = None,
) -> "ObjectStore":
    """Build a store from environment (Neon env pull or AWS default chain)."""
    if boto3 is None:
        raise RuntimeError("boto3 is required: pip install boto3")

    resolved_backend = _resolve_backend(backend or os.environ.get("GYPSY_STORAGE_BACKEND"))
    resolved_bucket = (bucket or bucket_from_env()).strip()
    if not resolved_bucket:
        raise ValueError(
            "Set GYPSY_STORAGE_BUCKET or GYPSY_S3_BUCKET (Neon: asx-artifacts after deploy)"
        )

    endpoint_url = os.environ.get("AWS_ENDPOINT_URL_S3") or None
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None

    if resolved_backend == "neon" and not endpoint_url:
        raise ValueError(
            "Neon object storage requires AWS_ENDPOINT_URL_S3 (run neon env pull)"
        )
    if resolved_backend == "aws":
        endpoint_url = None

    cfg = StoreConfig(
        backend=resolved_backend,
        bucket=resolved_bucket,
        endpoint_url=endpoint_url,
        region=region,
    )
    return ObjectStore(cfg)


class ObjectStore:
    def __init__(self, config: StoreConfig) -> None:
        self.config = config
        kwargs: dict[str, object] = {}
        if config.endpoint_url:
            kwargs["endpoint_url"] = config.endpoint_url
        if config.region:
            kwargs["region_name"] = config.region
        # When targeting AWS S3, hide Neon endpoint env vars from botocore.
        saved_endpoint = (
            os.environ.pop("AWS_ENDPOINT_URL_S3", None)
            if not config.endpoint_url
            else None
        )
        try:
            self._client = boto3.client(
                "s3",
                config=Config(s3={"addressing_style": "path"}),
                **kwargs,
            )
        finally:
            if saved_endpoint is not None:
                os.environ["AWS_ENDPOINT_URL_S3"] = saved_endpoint

    def uri(self, key: str) -> str:
        return f"{self.config.uri_prefix}{key.lstrip('/')}"

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.config.bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def get_bytes(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self.config.bucket, Key=key)
        return resp["Body"].read()

    def get_text(self, key: str, encoding: str = "utf-8") -> str:
        return self.get_bytes(key).decode(encoding)

    def put_bytes(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        extra: dict[str, str] = {}
        if content_type:
            extra["ContentType"] = content_type
        elif guessed := mimetypes.guess_type(key)[0]:
            extra["ContentType"] = guessed
        self._client.put_object(
            Bucket=self.config.bucket, Key=key, Body=payload, **extra
        )

    def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> None:
        self.put_bytes(key, path.read_bytes(), content_type=content_type)

    def put_text(self, key: str, text: str, encoding: str = "utf-8") -> None:
        self.put_bytes(key, text.encode(encoding), content_type="text/plain; charset=utf-8")

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.config.bucket, Key=key)

    def list_keys(self, prefix: str) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key")
                if key:
                    yield key

    def upload_dir(self, local_dir: Path, key_prefix: str) -> int:
        """Upload all files under local_dir to key_prefix/ (mirrors aws s3 sync)."""
        local_dir = local_dir.resolve()
        prefix = key_prefix.rstrip("/") + "/"
        count = 0
        for path in local_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(local_dir).as_posix()
            self.put_file(prefix + rel, path)
            count += 1
        return count

    def download_prefix(self, key_prefix: str, local_dir: Path) -> int:
        local_dir.mkdir(parents=True, exist_ok=True)
        prefix = key_prefix.rstrip("/") + "/"
        count = 0
        for key in self.list_keys(prefix):
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :] if key.startswith(prefix) else key
            dest = local_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.get_bytes(key))
            count += 1
        return count

    def head(self, key: str) -> dict[str, object]:
        return self._client.head_object(Bucket=self.config.bucket, Key=key)

    def copy_from(
        self,
        source: "ObjectStore",
        key: str,
        *,
        skip_if_same_size: bool = True,
    ) -> str:
        """Copy one object from another store (same key). Returns skipped|copied."""
        if skip_if_same_size and self.exists(key):
            try:
                dest_head = self.head(key)
                src_head = source.head(key)
                if dest_head.get("ContentLength") == src_head.get("ContentLength"):
                    return "skipped"
            except ClientError:
                pass
        body = source.get_bytes(key)
        ct = None
        try:
            src_head = source.head(key)
            ct = src_head.get("ContentType")
        except ClientError:
            pass
        self.put_bytes(key, body, content_type=ct)
        return "copied"
