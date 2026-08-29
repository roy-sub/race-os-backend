"""Retry with exponential backoff for transient network failures.

A nine-course rebuild reads hundreds of megabytes over a proxy. A single
dropped connection eight minutes in should not lose the run, and a retry is
safe here because every read is a byte-range GET of immutable object storage:
the same request returns the same bytes, so retrying cannot change output.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

import requests

T = TypeVar("T")

#: Failures worth retrying: the connection dropped or the proxy hiccuped.
#: A 404 or a 403 is a real answer and is never retried.
TRANSIENT = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ProxyError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class TransientStatus(requests.exceptions.RequestException):
    """A response whose status code says `try again`."""


def with_retry(fn: Callable[[], T], attempts: int = 5, base_delay: float = 1.0,
               sleep: Callable[[float], None] = time.sleep) -> T:
    """Call `fn`, retrying transient failures with exponential backoff.

    Delays are fixed (1s, 2s, 4s, 8s), not jittered: a build should be as
    reproducible in its timing as it is in its output.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except (TRANSIENT + (TransientStatus,)) as exc:
            last = exc
            if attempt == attempts - 1:
                break
            sleep(base_delay * (2 ** attempt))
    raise RuntimeError(
        f"giving up after {attempts} attempts: {last}"
    ) from last


def check_status(response: requests.Response) -> requests.Response:
    """Raise `TransientStatus` for codes worth retrying, else raise_for_status."""
    if response.status_code in RETRYABLE_STATUS:
        raise TransientStatus(f"HTTP {response.status_code} from {response.url}")
    response.raise_for_status()
    return response
