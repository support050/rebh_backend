# --proxy-headers is required for the slowapi auth limits to key on the real
# visitor instead of the platform proxy (see app/core/limiter.py).
web: alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}"
