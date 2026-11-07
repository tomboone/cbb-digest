"""Raw source responses kept on the data volume for 24 h, so a re-run never refetches (spec §6).

Each entry is one file, `<root>/<source>/<key>`: a line of JSON metadata, then the response body
byte for byte. One file means one atomic write, so an entry is never half-stored.

The cache is only a politeness window, never a fallback: an expired entry is refetched, and a
failed refetch is the caller's source failure, not a reason to serve the old entry (spec §9.3).
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from cbb_digest.atomic import write_atomic
from cbb_digest.clock import Clock

TTL = timedelta(hours=24)

# Source names and keys become path segments: no separators, no leading dot.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CachedResponse:
    body: bytes
    status: int
    content_type: str
    url: str
    fetched_at: datetime


class ResponseCache:
    def __init__(self, root: Path, clock: Clock) -> None:
        self._root = root
        self._clock = clock

    def get_or_fetch(
        self,
        source: str,
        key: str,
        fetch_fn: Callable[[], httpx.Response],
        ttl: timedelta = TTL,
    ) -> CachedResponse:
        """The stored response if it is younger than `ttl`; otherwise call `fetch_fn` and store it.

        `fetch_fn` must raise for any response that is not worth keeping: whatever it returns is
        stored. An exception from it propagates and leaves the cache untouched.
        """
        path = self._path(source, key)
        cached = self._read(path)
        if cached is not None and self._clock.now() - cached.fetched_at < ttl:
            log.info("cache hit", extra={"source": source, "key": key})
            return cached

        response = fetch_fn()
        fresh = CachedResponse(
            body=response.content,
            status=response.status_code,
            content_type=response.headers.get("Content-Type", ""),
            url=str(response.request.url),
            fetched_at=self._clock.now(),
        )
        meta = {
            "status": fresh.status,
            "content_type": fresh.content_type,
            "url": fresh.url,
            "fetched_at": fresh.fetched_at.isoformat(),
        }
        write_atomic(path, json.dumps(meta).encode() + b"\n" + fresh.body)
        log.info("cache stored", extra={"source": source, "key": key, "bytes": len(fresh.body)})
        return fresh

    def peek(self, source: str, key: str, ttl: timedelta = TTL) -> CachedResponse | None:
        """The stored entry if it is younger than `ttl`, else `None`; never fetches. For whether
        an entry is there, or for its metadata, such as when it was fetched."""
        cached = self._read(self._path(source, key))
        if cached is None or self._clock.now() - cached.fetched_at >= ttl:
            return None
        return cached

    def evict(self, source: str, key: str) -> None:
        """Drop an entry, e.g. one whose body failed validation, so the next call refetches."""
        self._path(source, key).unlink(missing_ok=True)

    def _path(self, source: str, key: str) -> Path:
        for name in (source, key):
            if not _SAFE_NAME.fullmatch(name):
                raise ValueError(f"unsafe cache name: {name!r}")
        return self._root / source / key

    def _read(self, path: Path) -> CachedResponse | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        header, _, body = raw.partition(b"\n")
        try:
            meta = json.loads(header)
            return CachedResponse(
                body=body,
                status=int(meta["status"]),
                content_type=str(meta["content_type"]),
                url=str(meta["url"]),
                fetched_at=datetime.fromisoformat(meta["fetched_at"]),
            )
        except (ValueError, KeyError, TypeError):
            log.warning("unreadable cache entry", extra={"path": str(path)})
            return None
