"""
Local-first ChromeDriver resolution.

Why this exists
---------------
`ChromeDriverManager().install()` from webdriver-manager asks Google's
endpoints for the newest driver version on every cold call (and downloads it if
the version file is stale). In a scheduled scraping run that is a pointless
external round-trip: the driver binaries are ALREADY on disk under `~/.wdm`
from earlier runs, and a stale/mismatched download adds seconds to startup and
can fail outright when the network is restricted (Render CI, offline dev box).

This module resolves a driver from the local cache first, version-matched
against the installed Chrome when it can be detected, and only returns None so
the caller may fall back to the network path when the cache holds nothing
usable.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"
_DRIVER_BIN = "chromedriver.exe" if _IS_WINDOWS else "chromedriver"

# webdriver-manager's on-disk layout: ~/.wdm/drivers/chromedriver/<platform>/<version>/[...]
_WDM_ROOT = os.path.join(os.path.expanduser("~"), ".wdm", "drivers", "chromedriver")

_VERSION_RE = re.compile(r"(\d+(?:\.\d+){1,3})")

# Common Chrome install locations tried when no binary is passed in.
_CHROME_FALLBACK_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
)


def _version_key(version: str) -> tuple:
    """Numeric tuple used for newest-version sorting ('151.0.7922.76' -> (151,0,7922,76))."""
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0,)


def detect_chrome_major(chrome_bin: Optional[str] = None) -> Optional[str]:
    """Return the installed Chrome major version (e.g. '151') or None."""
    candidates = []
    if chrome_bin:
        candidates.append(chrome_bin)
    for env_key in ("CHROME_BIN", "GOOGLE_CHROME_BIN", "CHROMIUM_BIN"):
        val = os.environ.get(env_key)
        if val:
            candidates.append(val)
    candidates.extend(_CHROME_FALLBACK_PATHS)

    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            out = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",
            )
            text_out = (out.stdout or "") + (out.stderr or "")
        except Exception:  # missing binary, no exec permission, timeout
            continue
        match = _VERSION_RE.search(text_out)
        if match:
            return match.group(1).split(".")[0]
    return None


def _cached_drivers() -> list:
    """Return [(version_key, path), ...] for every chromedriver in the local cache."""
    if not os.path.isdir(_WDM_ROOT):
        return []
    found = []
    for path in glob.glob(os.path.join(_WDM_ROOT, "**", _DRIVER_BIN), recursive=True):
        if not os.path.isfile(path):
            continue
        match = _VERSION_RE.search(path.replace("\\", "/"))
        version = match.group(1) if match else "0"
        found.append((_version_key(version), path))
    return found


def resolve_cached_chromedriver(chrome_bin: Optional[str] = None) -> Optional[str]:
    """
    Pick a chromedriver binary from the local ~/.wdm cache.

    Preference order:
      1. Exact major-version match against the detected Chrome (avoids
         "session not created" version-mismatch errors).
      2. Newest cached build, when Chrome's version cannot be detected.
      3. None — cache empty; caller decides whether to hit the network.
    """
    cached = _cached_drivers()
    if not cached:
        logger.debug("No chromedriver found in local cache (%s)", _WDM_ROOT)
        return None

    cached.sort(key=lambda item: item[0], reverse=True)

    major = detect_chrome_major(chrome_bin)
    if major:
        for key, path in cached:
            if key and str(key[0]) == str(major):
                logger.debug("Using cached ChromeDriver %s (matches Chrome %s)", path, major)
                return path
        logger.debug("No cached ChromeDriver matches Chrome %s — falling back to newest cached", major)

    newest = cached[0][1]
    logger.debug("Using newest cached ChromeDriver: %s", newest)
    return newest
