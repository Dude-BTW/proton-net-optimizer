"""Small subprocess wrapper with timeouts and typed errors."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from pno.errors import HelperError, PnoError


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    args: Sequence[str],
    *,
    timeout: float = 20.0,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    error_factory: type[PnoError] = HelperError,
    error_detail: str = "",
) -> CommandResult:
    """Run a command without a shell and capture its output.

    Timeouts and missing executables raise a typed error instead of leaking
    ``subprocess`` exceptions.
    """
    started = time.monotonic()
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            list(args),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise error_factory(
            f"command not found: {args[0]}", detail=error_detail or str(exc)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise error_factory(
            f"command timed out: {args[0]}", detail=error_detail or str(exc)
        ) from exc
    except OSError as exc:
        raise error_factory(
            f"cannot run {args[0]}", detail=error_detail or str(exc)
        ) from exc
    return CommandResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=int((time.monotonic() - started) * 1000),
    )
