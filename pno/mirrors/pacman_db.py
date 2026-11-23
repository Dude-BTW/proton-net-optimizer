"""Inspect the local pacman sync databases without root.

A mirrorlist can be perfectly healthy while the local sync databases are days
old. pacman then asks for package versions that the mirrors have already
replaced and every mirror answers 404, which looks like a broken mirror set.
This module only measures the age of ``/var/lib/pacman/sync/*.db`` and the
spread between repositories; it never runs pacman.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SYNC_DIR = Path("/var/lib/pacman/sync")
HINT = "Run 'sudo pacman -Syu' before installing packages."
REASON_TOO_OLD = "too-old"
REASON_SKEWED = "skewed"


def format_age_hours(value: float | None) -> str:
    """Render an age as ``1d 3h``, ``5h 12m`` or ``42m``."""
    if value is None:
        return "unknown"
    minutes = max(0, int(round(value * 60.0)))
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@dataclass(frozen=True)
class SyncDatabase:
    name: str
    path: str
    modified_at: float
    age_hours: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "modified_at": self.modified_at,
            "age_hours": round(self.age_hours, 3),
        }


@dataclass(frozen=True)
class PacmanDbReport:
    available: bool
    checked_at: float
    databases: tuple[SyncDatabase, ...]
    oldest_age_hours: float | None
    skew_hours: float | None
    stale: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "checked_at": self.checked_at,
            "stale": self.stale,
            "reasons": list(self.reasons),
            "oldest_age_hours": (
                round(self.oldest_age_hours, 3)
                if self.oldest_age_hours is not None
                else None
            ),
            "skew_hours": (
                round(self.skew_hours, 3) if self.skew_hours is not None else None
            ),
            "databases": [database.to_dict() for database in self.databases],
        }


def describe(report: PacmanDbReport | Mapping[str, Any]) -> str:
    """Return an English one-line description of the sync database state."""
    payload: Mapping[str, Any]
    if isinstance(report, PacmanDbReport):
        payload = report.to_dict()
    else:
        payload = report
    if not payload.get("available"):
        return "no pacman sync databases found"
    databases = payload.get("databases") or []
    if not databases:
        return "no pacman sync databases found"
    if not payload.get("stale"):
        oldest = payload.get("oldest_age_hours")
        return f"sync databases are up to date (oldest {format_age_hours(oldest)})"
    reasons = payload.get("reasons") or []
    parts: list[str] = []
    if REASON_TOO_OLD in reasons:
        oldest_name = min(databases, key=lambda item: item.get("modified_at") or 0.0)
        parts.append(
            f"{oldest_name.get('name')}.db is "
            f"{format_age_hours(oldest_name.get('age_hours'))} old"
        )
    if REASON_SKEWED in reasons:
        parts.append(
            f"repositories differ by {format_age_hours(payload.get('skew_hours'))}"
        )
    if not parts:
        parts.append("sync databases need attention")
    return "; ".join(parts)


def read_sync_databases(
    directory: Path | str | None = None,
    *,
    now: float | None = None,
    max_age_hours: float = 24.0,
    max_skew_hours: float = 6.0,
) -> PacmanDbReport:
    """Measure the sync databases; missing or unreadable entries are skipped."""
    timestamp = now if now is not None else time.time()
    root = Path(directory) if directory is not None else SYNC_DIR
    databases: list[SyncDatabase] = []
    try:
        entries = sorted(root.glob("*.db"))
    except OSError:
        entries = []
    for entry in entries:
        try:
            modified_at = entry.stat().st_mtime
        except OSError:
            continue
        databases.append(
            SyncDatabase(
                name=entry.name[: -len(".db")],
                path=str(entry),
                modified_at=modified_at,
                age_hours=max(0.0, (timestamp - modified_at) / 3600.0),
            )
        )
    if not databases:
        return PacmanDbReport(
            available=False,
            checked_at=timestamp,
            databases=(),
            oldest_age_hours=None,
            skew_hours=None,
            stale=False,
            reasons=(),
        )
    ages = [database.age_hours for database in databases]
    oldest = max(ages)
    skew = max(ages) - min(ages)
    reasons: list[str] = []
    if max_age_hours > 0 and oldest > max_age_hours:
        reasons.append(REASON_TOO_OLD)
    if max_skew_hours > 0 and skew > max_skew_hours:
        reasons.append(REASON_SKEWED)
    return PacmanDbReport(
        available=True,
        checked_at=timestamp,
        databases=tuple(databases),
        oldest_age_hours=oldest,
        skew_hours=skew,
        stale=bool(reasons),
        reasons=tuple(reasons),
    )
