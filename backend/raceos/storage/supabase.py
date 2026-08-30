"""Supabase Storage implementation of :class:`~raceos.storage.base.StorageBackend`.

Storage only. This module must never import a Supabase auth SDK, and
``tests/unit/test_dependency_boundaries.py`` asserts that no such package is
even installed: RaceOS issues its own RS256 tokens, and Supabase provides
exactly two things — a Postgres database and an object store.

Buckets are addressed through the project URL and the service-role secret key.
Private objects are reached only through short-lived signed URLs; the public
bucket exists for ``assets/`` media and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from raceos.config import Settings, get_settings
from raceos.storage.base import ObjectNotFoundError, StorageBackend, StorageError, StoredObject


class SupabaseStorage(StorageBackend):
    """Objects in Supabase Storage, over its REST API."""

    def __init__(
        self, settings: Settings | None = None, client: httpx.Client | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._base = self._settings.supabase_url.rstrip("/")
        key = self._settings.supabase_secret_key.get_secret_value()
        self._client = client or httpx.Client(
            timeout=self._settings.storage_request_timeout_seconds,
            headers={
                "Authorization": f"Bearer {key}",
                "apikey": key,
            },
        )

    # -- helpers -------------------------------------------------------
    def _bucket_name(self, public: bool) -> str:
        return (
            self._settings.supabase_storage_bucket_public
            if public
            else self._settings.supabase_storage_bucket_private
        )

    def _object_url(self, key: str, public: bool, *, verb: str = "object") -> str:
        bucket = self._bucket_name(public)
        return f"{self._base}/storage/v1/{verb}/{bucket}/{quote(key.lstrip('/'))}"

    @staticmethod
    def _raise_for_status(response: httpx.Response, key: str) -> None:
        if response.status_code == 404:
            raise ObjectNotFoundError(key)
        if response.status_code >= 400:
            # The body can echo request headers on some errors, so it is not
            # included verbatim; the status and key are enough to act on and
            # the full response is available in the structured log.
            raise StorageError(
                f"storage operation on {key!r} failed with HTTP {response.status_code}"
            )

    # -- interface -----------------------------------------------------
    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        public: bool = False,
    ) -> StoredObject:
        response = self._client.post(
            self._object_url(key, public),
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        self._raise_for_status(response, key)
        return StoredObject(
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            created_at=datetime.now(UTC),
        )

    def get(self, key: str, *, public: bool = False) -> bytes:
        response = self._client.get(self._object_url(key, public))
        self._raise_for_status(response, key)
        return response.content

    def exists(self, key: str, *, public: bool = False) -> bool:
        response = self._client.request("HEAD", self._object_url(key, public))
        if response.status_code == 404:
            return False
        self._raise_for_status(response, key)
        return True

    def delete(self, key: str, *, public: bool = False) -> None:
        response = self._client.delete(self._object_url(key, public))
        if response.status_code == 404:
            return
        self._raise_for_status(response, key)

    def signed_download_url(self, key: str, *, expires_in: int | None = None) -> str:
        ttl = expires_in or self._settings.storage_signed_url_ttl_seconds
        response = self._client.post(
            self._object_url(key, public=False, verb="object/sign"),
            json={"expiresIn": ttl},
        )
        self._raise_for_status(response, key)
        signed = response.json().get("signedURL") or response.json().get("signedUrl")
        if not signed:
            raise StorageError(f"Supabase returned no signed URL for {key!r}")
        return f"{self._base}/storage/v1{signed}" if signed.startswith("/") else signed

    def signed_upload_url(self, key: str, *, expires_in: int | None = None) -> str:
        response = self._client.post(
            self._object_url(key, public=False, verb="object/upload/sign"),
            json={"expiresIn": expires_in or self._settings.storage_signed_url_ttl_seconds},
        )
        self._raise_for_status(response, key)
        signed = response.json().get("url") or response.json().get("signedUrl")
        if not signed:
            raise StorageError(f"Supabase returned no signed upload URL for {key!r}")
        return f"{self._base}/storage/v1{signed}" if signed.startswith("/") else signed

    def public_url(self, key: str) -> str:
        return f"{self._settings.public_media_base_url}/{key.lstrip('/')}"

    def health(self) -> dict[str, Any]:
        """List buckets; confirms credentials and reachability in one call."""
        response = self._client.get(f"{self._base}/storage/v1/bucket")
        if response.status_code >= 400:
            raise StorageError(f"storage unreachable: HTTP {response.status_code}")
        names = [b.get("name") for b in response.json()]
        required = {
            self._settings.supabase_storage_bucket_private,
            self._settings.supabase_storage_bucket_public,
        }
        missing = sorted(required - set(names))
        if missing:
            raise StorageError(f"configured bucket(s) do not exist: {missing}")
        return {"backend": "supabase", "buckets": sorted(n for n in names if n)}

    def close(self) -> None:
        self._client.close()
