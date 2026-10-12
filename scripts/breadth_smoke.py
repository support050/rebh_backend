# -*- coding: utf-8 -*-
"""breadth_smoke.py — prove app.main imports cleanly with the breadth router.

Imports the REAL FastAPI app object (module-level import only; the heavy
startup event — create_tables/redis/scheduler — does NOT fire on import) and
checks that all 10 REBH breadth endpoints are registered under /api/rebh/breadth.

Exit 0 = app boots + all 10 routes present; 1 otherwise.

Usage: backend\\venv\\Scripts\\python.exe scripts/breadth_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

EXPECTED = {
    "/api/rebh/breadth/today",
    "/api/rebh/breadth/series",
    "/api/rebh/breadth/events",
    "/api/rebh/breadth/signals",
    "/api/rebh/breadth/sectors",
    "/api/rebh/breadth/stocks",
    "/api/rebh/breadth/stockbee",
    "/api/rebh/breadth/ohlc",
    "/api/rebh/breadth/ohlc/{symbol}",
    "/api/rebh/breadth/meta",
}


def main() -> int:
    try:
        from app.main import app  # noqa: F401  (full import = boot smoke)
    except Exception as exc:  # pragma: no cover
        print(f"[FAIL] app.main import raised: {type(exc).__name__}: {exc}")
        return 1

    from app.main import app as _app

    routes = [r for r in _app.routes if getattr(r, "methods", None)]
    paths = {p for r in routes for p in [r.path] if "/rebh/breadth" in p}
    print(f"total app routes: {len(routes)}")
    print(f"breadth routes registered: {len(paths)}")
    for p in sorted(paths):
        print("  ", p)

    missing = EXPECTED - paths
    if missing:
        print(f"[FAIL] missing breadth endpoints: {sorted(missing)}")
        return 1
    print("[OK ] all 10 breadth endpoints present in live app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
