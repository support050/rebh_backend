"""
Cache payload compression (Part 3 / Phase 6) — opt-in-by-size, always readable.

Why this exists
---------------
The payloads this app caches are not small: one breadth snapshot or a 50 000-row
price history serialises to megabytes, and the measured worst case that started
this whole review was a **9.7 MB JSONB row**. Every byte of that is paid twice —
once on the wire from Postgres, once on the wire from Redis, and on Render the
Redis instance and the API sit in different places than the developer's laptop,
so egress and latency are real money, not micro-optimisation.

Three constraints shape the design, and each one is here because breaking it
would be worse than not compressing:

1. **Old values must stay readable.** The codec never rewrites a key it did not
   write, and it identifies its own output by a leading marker that a JSON
   payload can never produce (``json.dumps`` output starts with ``{``, ``[``,
   ``"``, a digit, ``t``/``f``/``n`` — never a NUL). So a value cached before
   this module shipped is read back exactly as before, with no migration, no
   flush and no deploy ordering constraint.
2. **The client is created with ``decode_responses=True``** (see
   ``app/core/redis.py:_connect``). Redis is binary-safe, but redis-py is not:
   handing it raw gzip bytes means every read raises ``UnicodeDecodeError`` on
   the way back. Compressed bytes are therefore **Base64 encoded** — the stored
   value stays pure ASCII and round-trips through a text-only client. ``decode``
   also accepts ``bytes``, so flipping that flag off one day does not brick the
   cache; the alternative — a second binary-safe client — would put two
   connection pools in front of one instance, which is exactly the kind of split
   the auth keyspace was given its own instance to avoid.
3. **Compression must never make a payload bigger.** Base64 costs 4 bytes per 3.
   A stream that compresses poorly (already-compressed images, high-entropy
   tokens) would grow, so the compressed form is kept only when it is genuinely
   smaller than the plaintext it replaces. This is decided per value, not by a
   global setting.

Failure direction: encoding is wrapped and falls back to storing the plaintext;
decoding a *marked* value that cannot be decompressed returns ``None``, which the
caller treats as a cache MISS and recomputes. A corrupt payload must never be
served to a user as if it were data, and it must never be 500.

Algorithm: zstd when the optional ``zstandard`` package is importable, otherwise
gzip from the standard library (zero new dependencies; ``zlib`` is always
there). The two are distinguishable from the marker, so a value written by one
backend is readable after the other is installed — and mid-rollout.
"""

from __future__ import annotations

import base64
import gzip
import logging
from typing import Any, Dict, Optional, Tuple

from app.core.cache_config import (
    COMPRESS_GZIP_LEVEL,
    COMPRESS_MIN_BYTES,
    COMPRESS_ZSTD_LEVEL,
)

logger = logging.getLogger(__name__)

# Leading NUL = "written by this module". Chosen because it is impossible at the
# start of any json.dumps output, so no pre-existing cached value can be
# misread as ours, and no value we store can be mistaken for plain JSON.
MARKER_PREFIX = "\x00"
GZIP_MARKER = "\x00gz1:"
ZSTD_MARKER = "\x00zd1:"

_MARKERS = {GZIP_MARKER: "gzip", ZSTD_MARKER: "zstd"}

# zstandard is OPTIONAL (an unlisted extra, on purpose: a build must not fail
# because a wheel is unavailable). Absent it, gzip carries the load.
try:                                                # pragma: no cover - import env
    import zstandard as _zstdmodule                 # type: ignore
except Exception:                                   # noqa: BLE001 - any import breakage
    _zstdmodule = None


def algorithm() -> str:
    """Which codec compressed payloads will be written with, right now."""
    return "zstd" if _zstdmodule is not None else "gzip"


# In-process counters only (no metrics dependency). These are the numbers the
# log line is built from, and they are what makes "is compression earning its
# keep?" answerable without touching production.
CODEC_STATS: Dict[str, int] = {
    "compressions": 0,
    "raw_bytes": 0,
    "stored_bytes": 0,
    "corrupt_reads": 0,
}


def reset_codec_stats() -> None:
    """Zero the counters (tests; and a long-lived process may want a clean window)."""
    for key in CODEC_STATS:
        CODEC_STATS[key] = 0


# ── byte-level primitives (isolated so both branches are testable) ─────────

def _compress_bytes(data: bytes, algo: str) -> bytes:
    if algo == "zstd":
        if _zstdmodule is None:
            raise RuntimeError("zstandard is not installed")
        return _zstdmodule.ZstdCompressor(level=COMPRESS_ZSTD_LEVEL).compress(data)
    return gzip.compress(data, compresslevel=COMPRESS_GZIP_LEVEL)


def _decompress_bytes(data: bytes, algo: str) -> bytes:
    if algo == "zstd":
        if _zstdmodule is None:
            # A zstd value can only exist if the module was there when it was
            # written. Losing it again (a rebuilt image without the extra) must
            # read as a miss, not as garbage — so raise, and let decode() map it.
            raise RuntimeError("zstandard is not installed")
        return _zstdmodule.ZstdDecompressor().decompress(data)
    return gzip.decompress(data)


def _choose_algorithm() -> str:
    return "zstd" if _zstdmodule is not None else "gzip"


# ── value-level API used by app/core/redis.py ─────────────────────────────

def encode(serialized: str, *, min_bytes: Optional[int] = None,
           algo: Optional[str] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return ``(payload_to_store, meta)``; ``meta`` is None when nothing was done.

    ``serialized`` is the JSON text ``RedisCache.set()`` already produced, so
    there is exactly one serialisation per write and the codec never re-encodes
    a Python object (a second ``json.dumps`` would be a second place where
    floats and key order could drift).
    """
    threshold = COMPRESS_MIN_BYTES if min_bytes is None else int(min_bytes)
    raw_bytes = len(serialized.encode("utf-8"))
    if threshold <= 0 or raw_bytes < threshold:
        return serialized, None

    algorithm = algo or _choose_algorithm()
    try:
        packed = _compress_bytes(serialized.encode("utf-8"), algorithm)
        # ASCII on the wire: decode_responses=True would choke on raw gzip bytes.
        # The marker already carries the leading NUL — prefixing it again would
        # produce "\x00\x00gz1:" that decode() could not recognise, which is how
        # this line was first written and is exactly why it is spelled out here.
        payload = {"zstd": ZSTD_MARKER, "gzip": GZIP_MARKER}[algorithm] + \
            base64.b64encode(packed).decode("ascii")
    except Exception as e:                          # noqa: BLE001 - store plaintext
        logger.warning("cache_payload_compress_failed algo=%s bytes=%s (%s) — "
                       "storing plaintext", algorithm, raw_bytes, type(e).__name__)
        return serialized, None

    stored_bytes = len(payload)
    if stored_bytes >= raw_bytes:
        # Incompressible payload: paying the Base64 overhead would make the
        # value we ship LARGER than the one we read out of Postgres.
        return serialized, None

    meta = {
        "algo": algorithm,
        "raw_bytes": raw_bytes,
        "stored_bytes": stored_bytes,
        "saved_bytes": raw_bytes - stored_bytes,
    }
    CODEC_STATS["compressions"] += 1
    CODEC_STATS["raw_bytes"] += raw_bytes
    CODEC_STATS["stored_bytes"] += stored_bytes
    return payload, meta


def decode(stored: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """Return ``(plaintext_or_None, meta)`` for a value read back from Redis.

    Three outcomes, and the caller must not confuse the last two:

      * ``(value, None)``   — not written by this module (legacy or plaintext):
        hand it straight to ``json.loads`` like always;
      * ``(text, meta)``    — decompressed, carry on;
      * ``(None, meta)``    — it WAS ours and it will not decompress. The caller
        must treat this as a MISS. Returning the Base64 blob instead would serve
        a wall of garbage as if it were an API payload.
    """
    if isinstance(stored, (bytes, bytearray)):
        # A binary-safe client: accept it so decode_responses is not a load-
        # bearing dependency of this module.
        try:
            stored = bytes(stored).decode("utf-8")
        except UnicodeDecodeError:
            text = bytes(stored).decode("latin-1")
            return text, None
    if not isinstance(stored, str) or not stored.startswith(MARKER_PREFIX):
        return stored, None

    marker = next((m for m in _MARKERS if stored.startswith(m)), None)
    if marker is None:
        # A leading NUL that this module did not write: not ours to interpret.
        return stored, None
    algorithm = _MARKERS[marker]

    try:
        # Everything after the marker is Base64 (whose alphabet contains no ":"),
        # so the marker boundary is unambiguous.
        plaintext = _decompress_bytes(
            base64.b64decode(stored[len(marker):], validate=True), algorithm)
        text = plaintext.decode("utf-8")
    except Exception as e:                          # noqa: BLE001 - corrupt = miss
        # binascii.Error/ValueError (bad Base64), zlib/gzip errors (truncated
        # frame), UnicodeDecodeError (not UTF-8 any more) and RuntimeError (the
        # zstd extra disappeared under us). None of them may reach the user as a
        # 500 and none may be returned as if the blob were data.
        CODEC_STATS["corrupt_reads"] += 1
        logger.warning("cache_payload_decompress_failed algo=%s chars=%s (%s) — "
                       "treating as a cache miss", algorithm, len(stored),
                       type(e).__name__)
        return None, {"algo": algorithm, "error": type(e).__name__}

    return text, {
        "algo": algorithm,
        "stored_bytes": len(stored),
        "raw_bytes": len(text.encode("utf-8")),
    }


def is_compressed(stored: Any) -> bool:
    """Does this stored value carry one of our markers? (tests, admin preview.)"""
    if isinstance(stored, (bytes, bytearray)):
        try:
            stored = bytes(stored).decode("utf-8")
        except UnicodeDecodeError:
            return False
    return isinstance(stored, str) and any(
        stored.startswith(marker) for marker in _MARKERS)
