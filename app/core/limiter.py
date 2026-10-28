from slowapi import Limiter
from slowapi.util import get_remote_address

# Rate limiting is ENFORCED PER ROUTE by @limiter.limit(...) — see the decorated
# endpoints in app/api/routes/{auth,admin,rs,rs_v2}.py. Two things had to be true
# for those decorators to actually count:
#
# 1. app.state.limiter + the RateLimitExceeded handler must be registered
#    (they are, in app/main.py). Without them the decorator is inert.
# 2. The key must be the REAL client. get_remote_address returns
#    request.client.host, which is only the visitor's IP if uvicorn was started
#    with --proxy-headers (Procfile / Dockerfile). Behind Render every socket is
#    the platform proxy, so a single shared bucket meant "5 login attempts per
#    minute for the whole planet" — the limit looked strict but was really an
#    outage waiting to happen.
#
# No `default_limits` here on purpose: slowapi only applies them through
# SlowAPIMiddleware, and registering that middleware would silently cap EVERY
# route at one global number — including the data-heavy pages (terminal boards,
# RS HUB, screeners) that legitimately fire dozens of requests per paint. If a
# blanket limit is ever wanted, set it per router with explicit values instead.
limiter = Limiter(key_func=get_remote_address)
