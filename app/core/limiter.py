"""
Rate limiter — one bucket per visitor, shared by every worker.

Rate limiting is ENFORCED PER ROUTE by ``@limiter.limit(...)`` — see the
decorated endpoints in app/api/routes/{auth,admin,rs,rs_v2}.py. Four things have
to be true for those decorators to actually count:

1. ``app.state.limiter`` + the ``RateLimitExceeded`` handler must be registered
   (they are, in app/main.py). Without them the decorator is inert.
2. The key must be the REAL client. ``get_remote_address`` returns
   ``request.client.host``, which is only the visitor's IP if uvicorn was started
   with ``--proxy-headers`` (Procfile / Dockerfile). Behind Render every socket is
   the platform proxy, so a single shared bucket meant "5 login attempts per
   minute for the whole planet" — the limit looked strict but was really an
   outage waiting to happen.
3. (Part 3 / Phase 8) The counters must live where every worker can see them.
   slowapi's default storage is ``memory://``, i.e. one dict per process. With
   more than one web replica — or ``uvicorn --workers N`` — a "5/minute" login
   limit is really "5/minute per worker", so an attacker simply re-tries until
   they hit a fresh process. The buckets therefore go in Redis (the instance
   already configured as ``REDIS_URL``), which is also what makes them survive a
   deploy instead of resetting on every restart.
4. Getting there must not add a new way to fail. Two mechanisms, one at import
   and one at request time, and both are fail-OPEN by design: a rate limiter
   that answers 500 when its own storage is down converts a nuisance into an
   outage, and it is the limiter that protects the login endpoint.
     * import time: a short TCP probe decides the initial URI. Unreachable (or
       unset) Redis ⇒ ``memory://``, exactly the previous behaviour;
     * request time: ``in_memory_fallback_enabled=True`` makes slowapi catch any
       storage error, mark the backend dead, serve the request from per-process
       buckets, and periodically ``check()`` the Redis again ("Rate limit storage
       recovered"). ``swallow_errors=True`` covers whatever that path cannot
       catch. ``RateLimitExceeded`` is never swallowed — it is the whole point.

Algorithm
---------
``strategy="fixed-window"`` — deliberate, not a default left alone. It counts
hits in the calendar window (``5/minute`` = 5 per wall-clock minute boundary),
so the worst burst is 2× the limit when a window flips immediately after another
fills. That is acceptable here because every limit in this app guards an
operation that is *already* cheap and idempotent-safe (login, OTP, admin writes)
and the cost of over-rejecting a real user is a retry, not money.
``strategy="moving-window"`` is the upgrade if a limit ever needs to be exact:
it costs one more Redis key per window slot, and ``limits`` supports it on both
RedisStorage (Lua) and MemoryStorage. Switching is a one-word change here; the
stored bucket layout changes with it, so the flip is a deploy, not a hot edit.

No ``default_limits`` here on purpose: slowapi only applies them through
SlowAPIMiddleware, and registering that middleware would silently cap EVERY route
at one global number — including the data-heavy pages (terminal boards, RS HUB,
screeners) that legitimately fire dozens of requests per paint. If a blanket
limit is ever wanted, set it per router with explicit values instead.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Optional, Tuple
from urllib.parse import urlsplit

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.cache_config import (
    RATE_LIMIT_KEY_PREFIX,
    RATE_LIMIT_PROBE_TIMEOUT_SECONDS,
    RATE_LIMIT_SOCKET_CONNECT_TIMEOUT_SECONDS,
    RATE_LIMIT_SOCKET_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

MEMORY_STORAGE = "memory://"

# Set RATE_LIMIT_STORAGE_URI to force a mode: "memory://" keeps the counters
# per-process (unit tests that must not see another run's buckets), a redis://
# URI pins Redis storage even when the probe would have said no.
STORAGE_URI_ENV = "RATE_LIMIT_STORAGE_URI"


def _redis_url() -> Optional[str]:
    """The configured Redis target, or None.

    ``settings`` first (it owns .env parsing), ``REDIS_URL`` as the fallback so a
    bare script or a misconfigured import still gets a real answer instead of
    raising at import — this module is imported by app/main.py, and an exception
    here is a server that does not boot.
    """
    try:
        from app.core.config import settings
        url = getattr(settings, "REDIS_URL", None)
        if url:
            return str(url)
    except Exception as e:                            # noqa: BLE001 - must not block boot
        logger.warning("limiter: settings unavailable (%s) — reading REDIS_URL from env",
                       type(e).__name__)
    return os.getenv("REDIS_URL") or None


def _split_target(url: str) -> Tuple[Optional[str], Optional[int]]:
    """(host, port) for a redis:// URI. Unix sockets have neither."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return None, None
        port = parts.port or (6379 if parts.scheme in ("redis", "rediss") else None)
        return host, port
    except (ValueError, AttributeError):
        return None, None


def _redis_reachable(url: str, timeout: float = RATE_LIMIT_PROBE_TIMEOUT_SECONDS) -> bool:
    """Can a TCP socket open to the Redis host? (No Redis commands are sent.)

    Deliberately a connect, not a PING: a PING needs the password and a real
    client, and this is only deciding between two storage URIs at startup. A
    target with no host to probe (unix socket) is taken at face value, because
    guessing "unreachable" there would silently turn shared limits off.
    """
    host, port = _split_target(url)
    if not host or not port:
        return True
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError as e:
        logger.warning("limiter: rate-limit storage probe failed host=%s port=%s (%s)",
                       host, port, type(e).__name__)
        return False


def _use_memory() -> bool:
    """Unit tests must not inherit another run's counters.

    Under pytest the buckets stay per-process and die with the process. Nothing
    about production behaviour changes; the alternative — sharing live Redis
    counters with a test client — would make auth rate-limit tests depend on how
    recently the suite last ran.
    """
    return bool(os.getenv("PYTEST_CURRENT_TEST"))


def resolve_storage_uri() -> str:
    """Decide the storage URI. One probe, no side effects, safe to call in tests.

    The limiter is built with the value returned here ONCE at import (see
    ``STORAGE_URI`` below): re-probing on every call would put
    ``RATE_LIMIT_PROBE_TIMEOUT_SECONDS`` of startup back on each caller that asks
    "where do my counters live?" — which is exactly what /health and the logs do.
    """
    forced = os.getenv(STORAGE_URI_ENV)
    if forced:
        return forced
    if _use_memory():
        return MEMORY_STORAGE
    url = _redis_url()
    return url if url and _redis_reachable(url) else MEMORY_STORAGE


def describe_storage(uri: Optional[str] = None) -> str:
    """Credential-free, loggable statement of where the counters live."""
    uri = STORAGE_URI if uri is None else uri
    if uri == MEMORY_STORAGE:
        return "memory:// (per-process)"
    host, port = _split_target(uri)
    return f"redis://{host}:{port} (shared across workers)" if host else uri


# Redis storage timeouts: without them a Redis that accepts the socket but never
# answers makes every rate-limited request pay the kernel's connect timeout,
# which is the "login is slow whenever the cache is sick" bug Part 1 already
# removed from the data path. Same lesson, applied to the limiter.
_STORAGE_OPTIONS = {
    "socket_connect_timeout": RATE_LIMIT_SOCKET_CONNECT_TIMEOUT_SECONDS,
    "socket_timeout": RATE_LIMIT_SOCKET_TIMEOUT_SECONDS,
}

_uri = resolve_storage_uri()

#: What this process was built with. Read it instead of re-probing.
STORAGE_URI = _uri

limiter = Limiter(
    key_func=get_remote_address,
    strategy="fixed-window",              # see "Algorithm" in the docstring
    storage_uri=_uri,
    storage_options=_STORAGE_OPTIONS if _uri != MEMORY_STORAGE else {},
    key_prefix=RATE_LIMIT_KEY_PREFIX,
    # Fail-open, both layers (docstring point 4). Neither one can silence a
    # real RateLimitExceeded.
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)

logger.info("limiter: storage=%s strategy=fixed-window (in-memory fallback ready)",
            "shared-redis" if _uri != MEMORY_STORAGE else "memory://")
