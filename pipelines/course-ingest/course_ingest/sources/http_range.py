"""A seekable file-like object over an HTTPS object, using range requests.

Exists so pyarrow can read a 470 MB Parquet footer, and then only the row groups
that matter, without downloading the file. Reading the whole Overture
transportation theme would be 72 GB; a course bbox is around 80 MB.
"""
from __future__ import annotations

import io

import requests

from .retry import check_status, with_retry


class HttpRangeFile(io.RawIOBase):
    def __init__(self, url: str, session: requests.Session | None = None, timeout: int = 180) -> None:
        self.url = url
        self.timeout = timeout
        self._session = session or requests.Session()
        self._pos = 0
        head = with_retry(
            lambda: check_status(
                self._session.head(url, timeout=timeout, allow_redirects=True)
            )
        )
        self.size = int(head.headers["Content-Length"])
        self.request_count = 0
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = self.size + offset
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.size - self._pos
        if size == 0 or self._pos >= self.size:
            return b""
        end = min(self._pos + size, self.size) - 1
        resp = with_retry(
            lambda: check_status(
                self._session.get(
                    self.url,
                    headers={"Range": f"bytes={self._pos}-{end}"},
                    timeout=self.timeout,
                )
            )
        )
        data = resp.content
        self.request_count += 1
        self.bytes_read += len(data)
        self._pos += len(data)
        return data

    def readinto(self, buffer) -> int:  # type: ignore[override]
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)
