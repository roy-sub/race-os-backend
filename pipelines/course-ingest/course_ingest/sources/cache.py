"""Content-addressed on-disk cache for remote reads.

Keyed by the full request identity, so a cache hit is provably the same bytes a
cache miss would have fetched. This is what makes a re-run cheap without making
it non-reproducible.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


class BlobCache:
    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / namespace / digest[:2] / f"{digest}.blob"

    def get(self, namespace: str, key: str) -> bytes | None:
        if not self.enabled:
            return None
        p = self._path(namespace, key)
        if p.exists():
            self.hits += 1
            return p.read_bytes()
        return None

    def put(self, namespace: str, key: str, data: bytes) -> None:
        if not self.enabled:
            return
        self.misses += 1
        p = self._path(namespace, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
