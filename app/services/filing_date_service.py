"""Publication-date extraction for official filings.

`company_official_filings.published_date` was 100 % NULL while the ingest schema
(`ScrapedFilingItem.published_date`) had carried the field for ages: the route
hard-set `date_obj = None` and threw the value away, and the ~21 k rows that came
from the local-file generator have a dummy `http://internal/<year>_<Section>_<Period>`
source which simply does not contain a date.

The one trustworthy source we do have is the real Tadawul URL, which embeds the
upload/publication timestamp:

    https://www.saudiexchange.sa/Resources/fsPdf/3203_0_2025-03-05_15-07-55_En.pdf
                                                 ^^^^^^^^^^

Everything in this module is deliberately conservative: a date is returned only
when it is literally present and a real calendar date. We never derive a
"publication date" from the fiscal year/period — that would put a fabricated
date on a financial screen, and a NULL is strictly better than a wrong answer.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable, Optional

# ISO first: the Tadawul URLs, the scraper payload and the JSON feeds all use it.
_ISO = re.compile(r"(?P<y>19\d{2}|20\d{2})-(?P<m>0[1-9]|1[0-2])-(?P<d>0[1-9]|[12]\d|3[01])")
# Day-first forms seen on tadawul Arabic pages: 05/03/2025 or 05-03-2025 or 5-3-2025
_DMY = re.compile(r"(?P<d>0?[1-9]|[12]\d|3[01])[/-](?P<m>0?[1-9]|1[0-2])[/-](?P<y>19\d{2}|20\d{2})")


def _to_date(y: str, m: str, d: str) -> Optional[date]:
    try:
        return datetime(int(y), int(m), int(d)).date()
    except ValueError:      # 2025-02-30 and friends
        return None


def parse_one(text_value: str) -> Optional[date]:
    """First plausible calendar date inside one string, or None."""
    if not text_value:
        return None
    m = _ISO.search(text_value)
    if m:
        return _to_date(m.group("y"), m.group("m"), m.group("d"))
    m = _DMY.search(text_value)
    if m:
        return _to_date(m.group("y"), m.group("m"), m.group("d"))
    return None


def extract_published_date(candidates: Iterable[Optional[str]]) -> Optional[date]:
    """Try each candidate string in order and return the first date found.

    Callers pass them most-authoritative first, e.g. the date the scraper read
    from the filings table, then the source URL.
    """
    for cand in candidates:
        if not cand:
            continue
        found = parse_one(str(cand))
        if found:
            return found
    return None


def from_source_url(source_url: Optional[str]) -> Optional[date]:
    """Backfill path: the date embedded in the original download URL.

    Dummy `http://internal/2024_Financial_Statements_Annual.pdf` names carry only a
    fiscal year, and both regexes require a month and a day, so they return None —
    which is the honest answer for those rows.
    """
    if not source_url:
        return None
    if source_url.startswith("http://internal/"):
        return None
    return parse_one(source_url)
