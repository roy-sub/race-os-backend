"""Object storage behind one interface.

V1 stores objects in Supabase Storage, but nothing outside this package knows
that. Callers depend on :class:`StorageBackend`, so moving to R2 or S3 later
is one new class and a config value, not a refactor — which is the whole
reason the interface exists rather than calling Supabase directly.

Two implementations ship:

:class:`~raceos.storage.supabase.SupabaseStorage`
    The real one. Private buckets with signed URLs for uploads and generated
    exports; a public path only for ``assets/``.

:class:`InMemoryStorage`
    Used by the test suite and by a local development run with no Supabase
    project. The brief requires the suite to run fully offline with no
    credentials, and every external service to sit behind an interface so it
    can be stubbed at the boundary — this is that stub for storage, and it is
    a real working implementation rather than a mock.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from raceos.config import Settings, get_settings


class StorageError(RuntimeError):
    """Any storage operation that did not succeed."""


class ObjectNotFoundError(StorageError):
    """The requested key does not exist."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    content_type: str
    created_at: datetime


class StorageBackend(ABC):
    """What the rest of the application is allowed to assume about storage."""

    @abstractmethod
    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        public: bool = False,
    ) -> StoredObject:
        """Store *data* at *key*. Overwrites."""

    @abstractmethod
    def get(self, key: str, *, public: bool = False) -> bytes:
        """Read the object at *key*. Raises :class:`ObjectNotFoundError`."""

    @abstractmethod
    def exists(self, key: str, *, public: bool = False) -> bool:
        """Whether *key* exists. Used by the weekly media-asset audit."""

    @abstractmethod
    def delete(self, key: str, *, public: bool = False) -> None:
        """Remove *key*. Succeeds whether or not it existed."""

    @abstractmethod
    def signed_download_url(self, key: str, *, expires_in: int | None = None) -> str:
        """A time-limited URL granting read access to a private object."""

    @abstractmethod
    def signed_upload_url(self, key: str, *, expires_in: int | None = None) -> str:
        """A time-limited URL granting write access to a private key."""

    @abstractmethod
    def public_url(self, key: str) -> str:
        """The stable public URL for an object in the public bucket."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Detail for ``/readyz``. Raises if storage is unusable."""


class InMemoryStorage(StorageBackend):
    """A complete, working backend that keeps objects in process memory.

    Not a mock: it enforces the same not-found semantics and the same
    public/private separation as the real one, so a test that passes against
    it is testing the caller's logic rather than a stub's indulgence. Signed
    URLs are synthetic but carry a real expiry, so expiry logic is testable
    offline.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._lock = threading.Lock()
        self._private: dict[str, tuple[bytes, str, datetime]] = {}
        self._public: dict[str, tuple[bytes, str, datetime]] = {}

    def _bucket(self, public: bool) -> dict[str, tuple[bytes, str, datetime]]:
        return self._public if public else self._private

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        public: bool = False,
    ) -> StoredObject:
        now = datetime.now(UTC)
        with self._lock:
            self._bucket(public)[key] = (data, content_type, now)
        return StoredObject(
            key=key, size_bytes=len(data), content_type=content_type, created_at=now
        )

    def get(self, key: str, *, public: bool = False) -> bytes:
        with self._lock:
            entry = self._bucket(public).get(key)
        if entry is None:
            raise ObjectNotFoundError(key)
        return entry[0]

    def exists(self, key: str, *, public: bool = False) -> bool:
        with self._lock:
            return key in self._bucket(public)

    def delete(self, key: str, *, public: bool = False) -> None:
        with self._lock:
            self._bucket(public).pop(key, None)

    def signed_download_url(self, key: str, *, expires_in: int | None = None) -> str:
        ttl = expires_in or self._settings.storage_signed_url_ttl_seconds
        expiry = int((datetime.now(UTC) + timedelta(seconds=ttl)).timestamp())
        return f"memory://private/{key}?op=download&expires={expiry}"

    def signed_upload_url(self, key: str, *, expires_in: int | None = None) -> str:
        ttl = expires_in or self._settings.storage_signed_url_ttl_seconds
        expiry = int((datetime.now(UTC) + timedelta(seconds=ttl)).timestamp())
        return f"memory://private/{key}?op=upload&expires={expiry}"

    def public_url(self, key: str) -> str:
        return f"{self._settings.public_media_base_url}/{key.lstrip('/')}"

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "backend": "in_memory",
                "private_objects": len(self._private),
                "public_objects": len(self._public),
            }

    def clear(self) -> None:
        """Test helper: forget everything."""
        with self._lock:
            self._private.clear()
            self._public.clear()


_backend: StorageBackend | None = None


def get_storage_backend(settings: Settings | None = None) -> StorageBackend:
    """The process-wide storage backend.

    Chooses by configuration rather than by environment name: if a Supabase
    secret key is present, the real backend is used; otherwise the in-memory
    one is, which is what makes a local run and the test suite work with no
    credentials at all. A staging or production boot without that key has
    already been refused by :class:`~raceos.config.Settings`, so this cannot
    silently downgrade a real deployment to in-memory storage.
    """
    global _backend
    if _backend is not None:
        return _backend

    settings = settings or get_settings()
    if settings.supabase_secret_key.get_secret_value().strip():
        from raceos.storage.supabase import SupabaseStorage

        _backend = SupabaseStorage(settings)
    else:
        _backend = InMemoryStorage(settings)
    return _backend


def set_storage_backend(backend: StorageBackend | None) -> None:
    """Override the backend. For tests and for the seed script."""
    global _backend
    _backend = backend
