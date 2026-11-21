"""Measure mirror latency from the current network.

Arch's own ``score`` is a good prior, but it is measured from Arch's checker,
not from the user's current VPN exit. A short range request to ``core.db``
gives a real sample for the current route and also proves the mirror serves
files right now.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from pno.mirrors.models import Mirror, ProbeResult
from pno.mirrors.status import USER_AGENT

READ_BYTES = 1024


def probe_mirror(
    mirror: Mirror,
    *,
    timeout: float = 5.0,
    attempts: int = 2,
    opener: Callable[..., Any] | None = None,
) -> ProbeResult:
    best: float | None = None
    last_error: str | None = None
    last_status: int | None = None
    open_url = opener or urllib.request.urlopen
    for _ in range(max(1, attempts)):
        request = urllib.request.Request(
            mirror.probe_url,
            headers={
                "User-Agent": USER_AGENT,
                "Range": f"bytes=0-{READ_BYTES - 1}",
                "Accept": "*/*",
            },
        )
        started = time.monotonic()
        try:
            with open_url(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 0) or 0)
                chunk = response.read(READ_BYTES)
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_error = f"HTTP {exc.code}"
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(getattr(exc, "reason", exc))
            continue
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if status not in (200, 206):
            last_status = status
            last_error = f"HTTP {status}"
            continue
        if not chunk:
            last_status = status
            last_error = "empty response"
            continue
        if best is None or elapsed_ms < best:
            best = elapsed_ms
        last_status = status
    if best is not None:
        return ProbeResult(
            mirror=mirror, ok=True, latency_ms=best, status_code=last_status
        )
    return ProbeResult(
        mirror=mirror,
        ok=False,
        latency_ms=None,
        status_code=last_status,
        error=last_error or "probe failed",
    )


def probe_many(
    mirrors: Iterable[Mirror],
    *,
    timeout: float = 5.0,
    attempts: int = 2,
    workers: int = 12,
    probe: Callable[..., ProbeResult] = probe_mirror,
) -> list[ProbeResult]:
    items = list(mirrors)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        futures = [
            pool.submit(probe, mirror, timeout=timeout, attempts=attempts)
            for mirror in items
        ]
        results = [future.result() for future in futures]
    results.sort(key=lambda result: result.mirror.url)
    return results
