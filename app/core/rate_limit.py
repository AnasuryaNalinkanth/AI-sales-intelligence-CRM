"""Lightweight thread-safe in-memory rate limiter for expensive AI endpoints.

DEPLOYMENT NOTE:
This sliding-window limiter stores state in process memory and is suitable for
single-process local development and single-worker deployments. Multi-worker or
horizontally scaled production deployments require shared state (such as Redis
or an API gateway rate limiter) so request quotas are enforced across instances.
"""

from collections import deque
from threading import RLock
import time

from fastapi import Depends, Request

from app.core.config import settings
from app.core.exceptions import APIError
from app.core.security import get_current_user
from app.models import User


class InMemorySlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter keyed by user/client identity."""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = {}
        self._lock = RLock()

    def check(
        self,
        key: str,
        *,
        max_requests: int | None = None,
        window_seconds: int | None = None,
    ) -> None:
        limit = max_requests if max_requests is not None else settings.ai_rate_limit_requests
        window = (
            window_seconds
            if window_seconds is not None
            else settings.ai_rate_limit_window_seconds
        )
        now = time.monotonic()
        cutoff = now - float(window)

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                raise APIError(
                    "Too many AI requests. Please wait before trying again.",
                    status_code=429,
                    code="rate_limit_exceeded",
                )

            bucket.append(now)

            # Opportunistically prune empty buckets to bound memory
            if len(self._buckets) > 1024:
                empty_keys = [
                    k for k, timestamps in self._buckets.items() if not timestamps or timestamps[-1] <= cutoff
                ]
                for empty_key in empty_keys:
                    self._buckets.pop(empty_key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


ai_rate_limiter = InMemorySlidingWindowRateLimiter()


def enforce_ai_rate_limit(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> None:
    client_host = request.client.host if request.client else "unknown"
    key = f"user:{current_user.user_id}:{client_host}"
    ai_rate_limiter.check(key)


def enforce_analytics_ask_rate_limit(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> None:
    # Same limits, separate bucket: analytics questions do not use up the agent chat allowance.
    client_host = request.client.host if request.client else "unknown"
    ai_rate_limiter.check(f"analytics-ask:user:{current_user.user_id}:{client_host}")
