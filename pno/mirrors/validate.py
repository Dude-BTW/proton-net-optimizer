"""Validation rules for a generated ``mirrorlist``.

The same rules are duplicated inside :mod:`pno.mirrors.apply_helper`, which is
copied to a root-owned location and must not import user-writable code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_BYTES = 128 * 1024
MAX_SERVERS = 100

SERVER_RE = re.compile(
    r"^Server = (https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+/\$repo/os/\$arch)$"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class Validation:
    ok: bool
    servers: tuple[str, ...] = ()
    error: str | None = None


def validate_mirrorlist(text: str) -> Validation:
    if not text.strip():
        return Validation(False, error="mirrorlist is empty")
    if len(text.encode("utf-8")) > MAX_BYTES:
        return Validation(False, error=f"mirrorlist exceeds {MAX_BYTES} bytes")
    if _CONTROL_RE.search(text):
        return Validation(False, error="mirrorlist contains control characters")

    servers: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = SERVER_RE.match(line)
        if not match:
            return Validation(False, error=f"invalid line {number}: {line[:120]}")
        servers.append(match.group(1))
    if not servers:
        return Validation(False, error="mirrorlist contains no Server entries")
    if len(servers) > MAX_SERVERS:
        return Validation(False, error=f"too many servers ({len(servers)})")
    if len(set(servers)) != len(servers):
        return Validation(False, error="duplicate Server entries")
    if any(server.endswith("//$repo/os/$arch") for server in servers):
        return Validation(False, error="empty host in Server entry")
    return Validation(True, servers=tuple(servers))
