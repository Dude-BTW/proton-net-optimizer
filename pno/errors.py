"""Typed errors.

Every failure the tool can explain leaves through one of these classes so the
CLI can print a stable machine-readable ``code`` and the log can classify it.
"""

from __future__ import annotations

from typing import Any


class PnoError(Exception):
    """Base class for all expected failures."""

    code = "pno_error"

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        hint: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.hint:
            payload["hint"] = self.hint
        return payload


class ConfigError(PnoError):
    code = "config_error"


class DataUnavailableError(PnoError):
    code = "data_unavailable"


class NetworkError(PnoError):
    code = "network_error"


class AuthError(PnoError):
    code = "auth_error"


class PrivilegeError(PnoError):
    code = "permission_denied"


class HelperError(PnoError):
    code = "helper_error"


class ProbeError(PnoError):
    code = "probe_failed"


class ApplyError(PnoError):
    code = "apply_failed"


class IpcError(PnoError):
    code = "ipc_error"


class VpnError(PnoError):
    code = "vpn_error"


class ProtocolError(PnoError):
    code = "protocol_error"


class UnsupportedError(PnoError):
    code = "unsupported"
