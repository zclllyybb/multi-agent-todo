"""Polling helpers for long-running regression flows."""

from __future__ import annotations

import time
from typing import Callable, TypeVar


T = TypeVar("T")


def wait_until(
    predicate: Callable[[], T],
    *,
    timeout_sec: float,
    poll_interval_sec: float,
    description: str,
) -> T:
    """Poll ``predicate`` until it returns a truthy value or timeout expires."""

    deadline = time.monotonic() + timeout_sec
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out after {timeout_sec:.1f}s while waiting for {description}."
            )
        time.sleep(poll_interval_sec)
