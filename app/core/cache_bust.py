"""
Cache eviction — the ONE implementation, safe to call from anywhere.

Why this module exists
----------------------
The invalidation logic had been copy-pasted four times (daily_market_update
step 10, run_analytics_pipeline.flush_redis_cache, scheduler._clear_* and the
admin /api/cache routes). Every copy swallowed connection errors as
``logger.info("Redis inactive locally — safe in dev mode")``, so a script that
wrote to the PRODUCTION Postgres silently skipped the cache bust that the
PRODUCTION API needed — and the only thing that actually worked was opening a
Render shell and running ``flush_all()`` by hand.

Two rules this module enforces
------------------------------
1. **Bust by namespace, never ``*``.** ``APP_CACHE_PREFIXES`` lists what the
   application caches; auth/session keys are structurally outside it, and every
   pattern is still filtered against ``AUTH_KEY_PREFIXES`` as a backstop.
2. **A failed bust is loud and it is data.** Nothing returns a bare ``None``.
   Callers get a report dict; the report says which Redis host was reached, how
   many keys went away, and why it failed if it did.

Import hygiene: only stdlib + ``redis`` + ``cache_config`` (pure constants).
That is what lets ``breadth_job.py``, which runs in the project ROOT venv with
no pydantic/settings available, call this file directly.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.cache_config import (
    APP_CACHE_PREFIXES,
    AUTH_KEY_PREFIXES,
    DAILY_MARKET_PREFIXES,
    PREFIX_ECONOMIC,
    PREFIX_MARKET_REPORTS,
    PREFIX_REBH,
    PREFIX_RS_HUB,
    PREFIX_SCRAPER,
    PREFIX_TERMINAL,
)

logger = logging.getLogger(__name__)

SCAN_COUNT = 500          # same cursor size the scheduler has always used
SOCKET_TIMEOUT_SECONDS = 5.0
HTTP_TIMEOUT_SECONDS = 20.0

# ── Groups: a human name for "what this kind of change invalidates" ────────
GROUPS: Dict[str, Tuple[str, ...]] = {
    # Stock/screener reads: prices, RS ratings, screeners, industry groups.
    "market": DAILY_MARKET_PREFIXES[:6],
    # Terminal boards + the REBH engine per-company contract.
    "terminal": (PREFIX_TERMINAL, PREFIX_REBH),
    # Narrow on purpose: a breadth rerun must not rebuild every screener page.
    "breadth": (f"{PREFIX_REBH}:breadth",),
    # Economic indicators (FRED/SP500/yield-curve/eurodollar/fedwatch).
    "economic": (PREFIX_ECONOMIC,),
    # Tadawul market-reports aggregations (TASI chart …).
    "reports": (PREFIX_MARKET_REPORTS,),
    # RS Hub published JSON bundle.
    "rs_hub": (PREFIX_RS_HUB,),
    # Scraped per-company financial statements.
    "financials": (PREFIX_SCRAPER,),
    # Everything the app caches. NOT "*": see rule 1 above.
    "all": APP_CACHE_PREFIXES,
}

# ── job_name → groups. Keys are the system_registry / JOB_CALLABLES names ──
# Over-busting costs latency; under-busting costs correctness (stale numbers).
# When in doubt a row lists the wider group.
JOB_GROUPS: Dict[str, Tuple[str, ...]] = {
    "daily_market_update": ("market", "terminal", "rs_hub"),
    # scripts/run_analytics_pipeline.py: the indicator layers over the same rows,
    # plus the report aggregates — the exact set its own flush used to hard-code.
    "analytics_pipeline": ("market", "terminal", "reports", "rs_hub"),
    "breadth_daily": ("breadth",),
    "engine_vintages": ("terminal",),
    "market_reports": ("reports",),
    "filings_importer": ("terminal", "financials"),
    "sukuk_importer": ("economic",),
    "macro_importer": ("economic",),
    "sp500": ("economic",),
    "sp500_pe": ("economic",),
    "sp500_ey": ("economic",),
    "treasury_gov": ("economic",),
    "manual:eurodollar": ("economic",),
    "manual:cme_fedwatch": ("economic",),
    "manual:gurufocus": ("economic",),
    "scraper:tasi_components": ("market", "terminal"),
    "export_rs_hub_data": ("rs_hub", "market"),
}

# Family rules, checked when a job name is not listed above. Without them a new
# fred:* / scraper:* key would silently fall through to "bust everything", which
# is the stampede this module exists to avoid.
JOB_GROUP_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("fred:", ("economic",)),
    ("scraper:", ("economic",)),
    ("manual:", ("economic",)),
)

# A job nobody mapped still gets a correct (heavy) bust.
FALLBACK_GROUPS: Tuple[str, ...] = ("all",)


def patterns_for(*, job: Optional[str] = None,
                 groups: Optional[Sequence[str]] = None,
                 namespaces: Optional[Sequence[str]] = None) -> Tuple[List[str], List[str]]:
    """Resolve a request into (glob patterns, notes).

    ``namespaces`` accepts raw prefix names (``"prices"``) or full globs
    (``"prices:latest:*"``) so the admin UI can stay precise without adding a
    new alias for every one-off case.
    """
    notes: List[str] = []
    resolved: List[str] = []

    if job:
        mapped = JOB_GROUPS.get(job)
        if mapped is None:
            for prefix, rule_groups in JOB_GROUP_RULES:
                if job.startswith(prefix):
                    mapped = rule_groups
                    notes.append(f"job '{job}' matched the '{prefix}*' rule "
                                 f"— add it to JOB_GROUPS to be explicit")
                    break
        if mapped is None:
            mapped = FALLBACK_GROUPS
            notes.append(f"job '{job}' is not in JOB_GROUPS — busted {FALLBACK_GROUPS}")
        resolved.extend(mapped)
    if groups:
        unknown = [g for g in groups if g not in GROUPS]
        if unknown:
            raise KeyError("unknown cache group(s): %s (valid: %s)"
                           % (", ".join(sorted(unknown)), ", ".join(sorted(GROUPS))))
        resolved.extend(groups)

    prefixes: List[str] = []
    for item in resolved:
        for prefix in GROUPS[item]:
            if prefix not in prefixes:
                prefixes.append(prefix)

    for raw in namespaces or ():
        raw = raw.strip()
        if not raw:
            continue
        glob = raw if ":" in raw else f"{raw}:*"
        if glob not in prefixes:
            prefixes.append(glob)

    if not prefixes:
        prefixes = list(GROUPS["all"])
        notes.append("nothing specified — busted every app cache namespace")

    patterns = [p if p.endswith("*") else f"{p}:*" for p in prefixes]
    return patterns, notes


def is_auth_key(key: str) -> bool:
    k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
    return any(k.startswith(p) for p in AUTH_KEY_PREFIXES)


def _normalise_stem(prefix: Any) -> str:
    """``"terminal"`` / ``"terminal:"`` / ``"terminal:*"`` -> ``"terminal"``."""
    return str(prefix or "").strip().replace(":*", "").rstrip(":").strip()


def is_skipped_key(key: str, skip_prefixes: Sequence[str]) -> bool:
    """True when a namespace was already rewritten by this job's warm-up.

    Phase 4 depends on this: refresh_namespace writes the hot keys and then
    sweeps the rest of its namespace, so the job's regular bust must not come
    behind it and delete exactly what was just warmed. Stem matching (not plain
    startswith) so skipping ``terminal`` cannot skip ``terminalia:*``.
    """
    k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
    for stem in (_normalise_stem(p) for p in (skip_prefixes or ())):
        if not stem:
            continue
        if k == stem or k.startswith(stem + ":"):
            return True
    return False


def sanitize_url(url: str) -> str:
    """Redis URLs can carry a password; reports and logs must not."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.rsplit('@', 1)[-1]}"


ENV_FILE = Path(__file__).resolve().parents[2] / ".env"      # .../backend/.env
_DOTENV_CACHE: Optional[Dict[str, str]] = None


def _dotenv() -> Dict[str, str]:
    """Parse backend/.env with stdlib, once per process.

    Why this exists at all: ``settings`` (pydantic-settings) reads the .env into
    its own object and does NOT export it to ``os.environ``. Every script here
    therefore had REDIS_URL/CACHE_BUST_API_URL visible only through ``settings``,
    which this module must not import (breadth_job runs in the root venv, where
    pydantic is not on the path). Reading the file directly is what lets a bare
    ``python -m app.core.cache_bust`` work from any cwd, with no web-app import.
    A real environment variable always wins — that is how Render overrides it.
    """
    global _DOTENV_CACHE
    if _DOTENV_CACHE is None:
        parsed: Dict[str, str] = {}
        try:
            for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().removeprefix("export ").strip()
                value = value.strip().strip('"').strip("'")
                if key and value:
                    parsed[key] = value
        except OSError:                            # no .env (container/CI): env vars only
            pass
        _DOTENV_CACHE = parsed
    return _DOTENV_CACHE


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(name) or _dotenv().get(name) or default


def _redis_url(override: Optional[str] = None) -> str:
    return str(override or _env("REDIS_URL") or "redis://localhost:6379/0")


def _client(url: Optional[str] = None, timeout: float = SOCKET_TIMEOUT_SECONDS):
    """Sync client — scripts, jobs and threadpool routes have no event loop."""
    import redis as sync_redis

    return sync_redis.from_url(
        _redis_url(url), decode_responses=True,
        socket_timeout=timeout, socket_connect_timeout=timeout,
    )


def evict(patterns: Sequence[str], *, url: Optional[str] = None,
          timeout: float = SOCKET_TIMEOUT_SECONDS,
          skip_prefixes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """SCAN+UNLINK every key matching ``patterns``. Never raises.

    Auth/session keys are skipped even if a pattern would have caught them, so
    a caller passing ``"*"`` cannot log anybody out.

    ``skip_prefixes`` additionally protects namespaces a caller has already
    refreshed (see is_skipped_key); it is reported, never silent.
    """
    report: Dict[str, Any] = {
        "channel": "local",
        "target": sanitize_url(_redis_url(url)),
        "reachable": False,
        "deleted": 0,
        "skipped_auth_keys": 0,
        "skipped_warm_keys": 0,
        "per_pattern": {},
        "error": None,
    }
    if not patterns:
        report["error"] = "ValueError: no patterns to evict"
        return report
    skip = tuple(skip_prefixes or ())

    started = time.perf_counter()
    try:
        client = _client(url, timeout)
    except Exception as exc:  # noqa: BLE001 — missing lib / bad URL is a fact
        report["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        report["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return report

    try:
        client.ping()
        report["reachable"] = True
        for pattern in patterns:
            deleted, skipped, warm_skipped, cursor = 0, 0, 0, 0
            while True:
                cursor, keys = client.scan(cursor=cursor, match=pattern, count=SCAN_COUNT)
                if keys:
                    candidates = [k for k in keys if not is_auth_key(k)]
                    skipped += len(keys) - len(candidates)
                    if skip:
                        warm_skipped += sum(1 for k in candidates
                                            if is_skipped_key(k, skip))
                        candidates = [k for k in candidates
                                      if not is_skipped_key(k, skip)]
                    if candidates:
                        client.unlink(*candidates)
                        deleted += len(candidates)
                if cursor == 0:
                    break
            report["per_pattern"][pattern] = deleted
            report["deleted"] += deleted
            report["skipped_auth_keys"] += skipped
            report["skipped_warm_keys"] += warm_skipped
    except Exception as exc:  # noqa: BLE001 — an unreachable cache is a fact, not a crash
        report["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        report["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return report


def info(*, url: Optional[str] = None, timeout: float = SOCKET_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Health + hit ratio of the cache this process would use.

    A fail-open cache is invisible without this: ``keyspace_hits`` staying at 0
    is the only sign that every request has been rebuilding from Postgres.
    """
    out: Dict[str, Any] = {
        "target": sanitize_url(_redis_url(url)),
        "reachable": False,
        "redis_version": None,
        "keyspace_hits": None,
        "keyspace_misses": None,
        "hit_ratio": None,
        "expired_keys": None,
        "evicted_keys": None,
        "used_memory_human": None,
        "maxmemory_policy": None,
        "key_counts": {},
        "error": None,
    }
    try:
        client = _client(url, timeout)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        return out
    try:
        client.ping()
        out["reachable"] = True
        raw = client.info()
        hits = int(raw.get("keyspace_hits") or 0)
        misses = int(raw.get("keyspace_misses") or 0)
        out.update({
            "redis_version": raw.get("redis_version"),
            "keyspace_hits": hits,
            "keyspace_misses": misses,
            "hit_ratio": round(hits / (hits + misses), 4) if (hits + misses) else None,
            "expired_keys": raw.get("expired_keys"),
            "evicted_keys": raw.get("evicted_keys"),
            "used_memory_human": raw.get("used_memory_human"),
            "maxmemory_policy": raw.get("maxmemory_policy"),
        })
        for prefix in APP_CACHE_PREFIXES:
            cursor, keys = client.scan(cursor=0, match=f"{prefix}:*", count=SCAN_COUNT)
            out["key_counts"][prefix] = len(keys)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def validate_patterns(raw: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Gate for anything arriving over the network: only app namespaces bust.

    A bare ``*`` (or a root this app does not own) is refused even with a valid
    internal key, because the instance is shared: sweeping it dry would take
    rate-limit buckets and any other app's keys with it, and stampede every
    page onto Postgres at once.

    Matching is on the whole stem, not its first colon-delimited word: two real
    namespaces contain a colon (``industry:groups``, ``rebh:breadth``), so a
    naive ``split(":")[0]`` check would refuse ``industry:groups:*`` — a
    legitimate bust blocked by the gate that exists to make busts reliable.
    """
    accepted: List[str] = []
    rejected: List[str] = []
    for item in raw:
        pattern = (item or "").strip()
        stem = pattern.split("*")[0].strip(":")
        covered = any(stem == prefix or stem.startswith(prefix + ":")
                      for prefix in APP_CACHE_PREFIXES)
        if not pattern or pattern == "*" or not stem or not covered:
            rejected.append(pattern or "(empty)")
            continue
        accepted.append(pattern if "*" in pattern else f"{pattern}:*")
    return accepted, rejected


def api_base_url(override: Optional[str] = None) -> Optional[str]:
    """Where the production API lives. Checked in order:

    explicit argument → CACHE_BUST_API_URL → API_BASE_URL → BACKEND_URL → None.

    The argument wins, so a staging box or a deliberate dry run can be targeted
    without editing .env (and so a test can never reach production by accident).
    """
    if override:
        return str(override).rstrip("/")
    for key in ("CACHE_BUST_API_URL", "API_BASE_URL", "BACKEND_URL"):
        value = _env(key)
        if value:
            return str(value).rstrip("/")
    return None


def internal_key(override: Optional[str] = None) -> Optional[str]:
    return override or _env("INTERNAL_API_KEY")


def is_deploy_host() -> bool:
    """True when this process IS the deployment (Render sets ``RENDER=true``).

    This is the only honest test for "am I standing next to the Redis the site
    reads". A laptop uvicorn can have a perfectly healthy local Redis — and
    busting it still leaves the deployed pages stale (measured 2026-11-04:
    engine_vintages Run-Now from the laptop logged a clean local bust while
    Render's cache kept the old payload). Everywhere but the deploy, the HTTP
    door is therefore mandatory, not a fallback.
    """
    return str(_env("RENDER") or "").strip().lower() in {"1", "true", "yes", "on"}


def notify_api(*, job: Optional[str] = None, groups: Optional[Sequence[str]] = None,
               namespaces: Optional[Sequence[str]] = None,
               base: Optional[str] = None, key: Optional[str] = None,
               timeout: float = HTTP_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Ask the deployed API to bust ITS Redis (the HTTP door).

    This is what makes a laptop script refresh the site: the script writes to
    the production database, so the bust has to happen next to the production
    cache — and the server already knows that URL. No Redis credentials ever
    leave the deploy.
    """
    patterns, _ = patterns_for(job=job, groups=groups, namespaces=namespaces)
    report: Dict[str, Any] = {
        "channel": "remote",
        "target": None,
        "reachable": False,
        "deleted": None,
        "error": None,
    }
    base = api_base_url(base)
    if not base:
        report["error"] = ("not configured: set CACHE_BUST_API_URL "
                           "(and INTERNAL_API_KEY) to use the HTTP door")
        return report
    key = internal_key(key)
    if not key:
        report["error"] = "not configured: INTERNAL_API_KEY is unset"
        return report

    report["target"] = base
    query = "&".join(f"patterns={urllib.parse.quote(p, safe=':')}" for p in patterns)
    url = f"{base}/api/internal/cache/bust?{query}"
    request = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"X-Internal-Key": key, "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        report["reachable"] = True
        report["deleted"] = payload.get("deleted")
        report["per_pattern"] = payload.get("per_pattern")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")[:200]
        except Exception:  # noqa: BLE001
            pass
        report["error"] = f"HTTP {exc.code}: {body or exc.reason}"
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    report["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return report


def bust(*, job: Optional[str] = None, groups: Optional[Sequence[str]] = None,
         namespaces: Optional[Sequence[str]] = None, local: bool = True,
         remote: Optional[bool] = None, url: Optional[str] = None,
         base: Optional[str] = None, key: Optional[str] = None,
         skip_prefixes: Optional[Sequence[str]] = None,
         log: bool = True) -> Dict[str, Any]:
    """Bust the cache wherever the readers actually look. Returns the full report.

    ``remote=None`` (the default) is a policy, not a guess:
      · ON the deploy (``RENDER=true``) → local only. The local Redis IS the
        cache the API reads; knocking on our own HTTP door would be the app
        calling itself.
      · ANYWHERE ELSE (laptop uvicorn, terminal script, CI) → ALWAYS knock the
        door, even when a local Redis is reachable and busted cleanly. That
        local cache is not the one behind the site, so skipping the door for a
        reachable local Redis silently left Render serving stale payloads
        (the 2026-11-04 engine_vintages bug this line retired).

    Pass ``remote=True`` / ``remote=False`` to force either way.

    ``skip_prefixes`` protects namespaces this caller already refreshed (Part 2
    / Phase 4: warm first, then evict the rest). It applies to THIS host's Redis
    only — the HTTP door takes patterns, not exclusions — so a run that has to
    use the door says so in ``notes`` instead of pretending the protection
    travelled with it.
    """
    patterns, notes = patterns_for(job=job, groups=groups, namespaces=namespaces)
    skip = tuple(skip_prefixes or ())

    channels: List[Dict[str, Any]] = []
    local_report: Optional[Dict[str, Any]] = None
    if local:
        local_report = evict(patterns, url=url, skip_prefixes=skip)
        channels.append(local_report)
    if remote is None:
        # Not the deploy → the door is mandatory, reachable local Redis or not.
        remote = not is_deploy_host()
    if remote:
        channels.append(notify_api(job=job, groups=groups, namespaces=namespaces,
                                   base=base, key=key))
        if skip:
            notes.append("skip_prefixes is local-only: the remote bust re-evicts "
                         "those namespaces, so a warm-up does not survive the "
                         "HTTP door")

    reached = [c for c in channels if c["reachable"]]
    result = {
        "ok": bool(reached),
        "patterns": patterns,
        "notes": notes,
        "skip_prefixes": list(skip),
        "channels": channels,
        "deleted": sum(int(c.get("deleted") or 0) for c in reached),
        "summary": "; ".join(
            f"{c['channel']}={c['deleted'] if c['reachable'] else 'UNREACHABLE'}"
            for c in channels) or "no channel attempted",
    }
    if log:
        _log_result(result, job=job)
    return result


def _log_result(result: Dict[str, Any], *, job: Optional[str]) -> None:
    """A bust that reached nothing is a WARNING, never an INFO 'safe'."""
    label = f" for {job}" if job else ""
    if result["ok"]:
        logger.info("cache busted%s: %s (patterns=%s)", label,
                    result["summary"], ",".join(result["patterns"]))
        # Partial success is not success: when one channel was attempted and
        # failed, the readers behind IT stay stale — say so beside the green
        # line instead of letting "ok" bury the gap (the laptop case: local
        # bust fine, door unreachable → Render keeps the old payload).
        for c in result["channels"]:
            if not c["reachable"]:
                logger.warning(
                    "cache-bust channel '%s' (%s) FAILED: %s — readers behind "
                    "that host keep the previous payload until the TTL lapses",
                    c["channel"], c.get("target") or "?", c.get("error"))
        for note in result["notes"]:
            logger.warning("cache-bust note: %s", note)
    else:
        reasons = "; ".join(str(c.get("error")) for c in result["channels"])
        logger.warning(
            "CACHE NOT BUSTED%s — data is written but readers will keep seeing "
            "the old payload until the TTL lapses. tried: %s | %s",
            label, result["summary"], reasons)


def main(argv: Optional[Iterable[str]] = None) -> int:
    """CLI so nobody has to open a Render shell and type python -c.

        python -m app.core.cache_bust --job breadth_daily
        python -m app.core.cache_bust --groups market,breadth
        python -m app.core.cache_bust --all --local-only

    Exit code 1 means nothing was reached — a script or CI step can then fail
    loudly instead of reporting success next to stale data.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Evict application cache namespaces")
    parser.add_argument("--job", help="bust what this job invalidates (JOB_GROUPS)")
    parser.add_argument("--groups", help="comma-separated group names")
    parser.add_argument("--namespaces", help="comma-separated prefixes or globs")
    parser.add_argument("--skip-prefixes", dest="skip_prefixes",
                        help="comma-separated namespaces to protect (already "
                             "warmed by this run; local channel only)")
    parser.add_argument("--all", action="store_true", help="bust every app namespace")
    parser.add_argument("--local-only", action="store_true", help="skip the HTTP door")
    parser.add_argument("--remote-only", action="store_true", help="skip this host's Redis")
    parser.add_argument("--base", help="override the API URL for the HTTP door "
                                       "(staging boxes, or a dry run against a "
                                       "port that is not listening)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    groups = [g.strip() for g in (args.groups or "").split(",") if g.strip()] or None
    namespaces = [n.strip() for n in (args.namespaces or "").split(",") if n.strip()] or None
    skip_prefixes = [s.strip() for s in (args.skip_prefixes or "").split(",") if s.strip()] or None
    if args.all:
        groups = tuple(sorted((groups or ()) + ("all",)))

    # Neither flag = the library default: bust here, and knock on the door only
    # if "here" turned out not to hold the cache.
    remote = True if args.remote_only else (False if args.local_only else None)
    result = bust(job=args.job, groups=list(groups) if groups else None,
                  namespaces=namespaces, base=args.base, skip_prefixes=skip_prefixes,
                  local=not args.remote_only, remote=remote)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("patterns : %s" % ",".join(result["patterns"]))
        for channel in result["channels"]:
            state = "ok" if channel["reachable"] else "FAILED"
            print("%-8s %s target=%s deleted=%s%s" % (
                channel["channel"], state, channel.get("target"), channel.get("deleted"),
                (" error=" + str(channel.get("error"))) if channel.get("error") else ""))
        for note in result["notes"]:
            print("note     %s" % note)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
