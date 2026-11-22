"""Read and write the settings owned by the Proton VPN applications.

The Proton GUI and CLI keep their user settings in
``$XDG_CONFIG_HOME/Proton/VPN/settings.json`` and the app-only preferences in
``app-config.json``. ``protonvpn connect`` loads the former right before it
builds the tunnel, so a value written here is honoured by the next connection.

This module deliberately edits only the keys the user asked for:

* the file is read as plain JSON and unknown keys are preserved;
* an absent key stays absent, so Proton keeps resolving its own default;
* nothing is written when the effective document would not change;
* a malformed document raises a typed error and is never overwritten.

No credentials are read, and personal values (application paths, pinned
servers) are never logged.
"""

from __future__ import annotations

import copy
import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pno import paths
from pno.errors import SettingsError
from pno.proc import CommandResult, run
from pno.state import atomic_write_text, exclusive_lock

Runner = Callable[..., CommandResult]

# Canonical names used by the CLI and JSON output. They intentionally differ
# from the numeric values stored by Proton.
NETSHIELD_VALUES = {
    "off": 0,
    "malware-only": 1,
    "malware-ads-trackers": 2,
}
NETSHIELD_NAMES = {value: name for name, value in NETSHIELD_VALUES.items()}

KILLSWITCH_VALUES = {
    "off": 0,
    "standard": 1,
    "advanced": 2,
}
KILLSWITCH_NAMES = {value: name for name, value in KILLSWITCH_VALUES.items()}

BOOL_WORDS = {
    "on": True,
    "off": False,
    "true": True,
    "false": False,
    "yes": True,
    "no": False,
    "1": True,
    "0": False,
}


@dataclass(frozen=True)
class ProtocolOption:
    id: str
    label: str
    family: str
    packet_capture: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "family": self.family,
            "packet_capture": self.packet_capture,
        }


# Fallback used when the Proton libraries cannot report their registry. The IDs
# are Proton's stable protocol contract; the live registry still decides which
# ones are actually available.
FALLBACK_PROTOCOLS = (
    ProtocolOption("protun-smart", "Smart", "protun", True),
    ProtocolOption("protun-udp", "Wireguard UDP", "protun", True),
    ProtocolOption("protun-tcp", "Wireguard TCP", "protun", True),
    ProtocolOption("protun-tls", "Stealth", "protun", True),
    ProtocolOption("wireguard", "WireGuard", "generic", False),
    ProtocolOption("openvpn-udp", "OpenVPN (UDP)", "generic", False),
    ProtocolOption("openvpn-tcp", "OpenVPN (TCP)", "generic", False),
)

PROTOCOL_ALIASES = {
    "smart": "protun-smart",
    "wireguard-udp": "protun-udp",
    "wireguard-tcp": "protun-tcp",
    "stealth": "protun-tls",
    "wireguard": "wireguard",
    "openvpn-udp": "openvpn-udp",
    "openvpn-tcp": "openvpn-tcp",
    "protun-smart": "protun-smart",
    "protun-udp": "protun-udp",
    "protun-tcp": "protun-tcp",
    "protun-tls": "protun-tls",
}

DEFAULT_PROTOCOL = "wireguard"
DEFAULT_KILLSWITCH = 0
DEFAULT_NETSHIELD = 0
DEFAULT_IPV6 = True
DEFAULT_TELEMETRY = True
DEFAULT_CRASH_REPORTS = True
DEFAULT_VPN_ACCELERATOR = True
DEFAULT_MODERATE_NAT = False
DEFAULT_PORT_FORWARDING = False

# Fields that Proton only lets the user change while disconnected. This mirrors
# the GUI: the protocol, kill switch and split tunneling widgets are disabled
# on an active connection.
DISCONNECTED_ONLY = {
    "protocol",
    "killswitch",
    "split_tunneling_enabled",
    "split_tunneling_mode",
    "split_tunneling_apps",
}


@dataclass(frozen=True)
class ProtonSettings:
    path: Path
    raw: dict[str, Any]
    exists: bool

    def canonical(self) -> dict[str, Any]:
        """Return the effective settings using the CLI/JSON vocabulary."""
        return {
            "protocol": self.protocol,
            "killswitch": self.killswitch,
            "ipv6": self.ipv6,
            "anonymous_crash_reports": self.anonymous_crash_reports,
            "telemetry": self.telemetry,
            "netshield": self.netshield,
            "vpn_accelerator": self.vpn_accelerator,
            "moderate_nat": self.moderate_nat,
            "port_forwarding": self.port_forwarding,
            "split_tunneling": {
                "enabled": self.split_tunneling_enabled,
                "mode": self.split_tunneling_mode,
                "app_paths": list(self.split_tunneling_apps),
                "ip_ranges": list(self.split_tunneling_ip_ranges),
            },
            "custom_dns": {
                "enabled": self.custom_dns_enabled,
                "ips": list(self.custom_dns_ips),
            },
        }

    # ------------------------------------------------------------- accessors
    @property
    def protocol(self) -> str:
        value = self.raw.get("protocol")
        return value if isinstance(value, str) and value else DEFAULT_PROTOCOL

    @property
    def killswitch(self) -> str:
        return KILLSWITCH_NAMES.get(
            _as_int(self.raw.get("killswitch"), DEFAULT_KILLSWITCH), "unknown"
        )

    @property
    def killswitch_value(self) -> int:
        return _as_int(self.raw.get("killswitch"), DEFAULT_KILLSWITCH)

    @property
    def ipv6(self) -> bool:
        return _as_bool(self.raw.get("ipv6"), DEFAULT_IPV6)

    @property
    def anonymous_crash_reports(self) -> bool:
        return _as_bool(self.raw.get("anonymous_crash_reports"), DEFAULT_CRASH_REPORTS)

    @property
    def telemetry(self) -> bool:
        return _as_bool(self.raw.get("telemetry"), DEFAULT_TELEMETRY)

    @property
    def netshield(self) -> str:
        features = _dict(self.raw.get("features"))
        value = _as_int(features.get("netshield"), DEFAULT_NETSHIELD)
        return NETSHIELD_NAMES.get(value, "unknown")

    @property
    def netshield_value(self) -> int:
        features = _dict(self.raw.get("features"))
        return _as_int(features.get("netshield"), DEFAULT_NETSHIELD)

    @property
    def vpn_accelerator(self) -> bool:
        return _features_bool(self.raw, "vpn_accelerator", DEFAULT_VPN_ACCELERATOR)

    @property
    def moderate_nat(self) -> bool:
        return _features_bool(self.raw, "moderate_nat", DEFAULT_MODERATE_NAT)

    @property
    def port_forwarding(self) -> bool:
        return _features_bool(self.raw, "port_forwarding", DEFAULT_PORT_FORWARDING)

    @property
    def split_tunneling_enabled(self) -> bool:
        return _as_bool(_split_group_view(self.raw).get("enabled"), False)

    @property
    def split_tunneling_mode(self) -> str:
        return _split_mode(self.raw)

    @property
    def split_tunneling_apps(self) -> tuple[str, ...]:
        return self.split_tunneling_apps_for(self.split_tunneling_mode)

    def split_tunneling_apps_for(self, mode: str) -> tuple[str, ...]:
        config = _split_config(self.raw, mode)
        return tuple(config.get("app_paths", ()))

    @property
    def split_tunneling_ip_ranges(self) -> tuple[str, ...]:
        return _split_config(self.raw, self.split_tunneling_mode).get("ip_ranges", ())

    @property
    def custom_dns_enabled(self) -> bool:
        return _as_bool(_dict(self.raw.get("custom_dns")).get("enabled"), False)

    @property
    def custom_dns_ips(self) -> tuple[str, ...]:
        entries = _dict(self.raw.get("custom_dns")).get("ip_list")
        if not isinstance(entries, list):
            return ()
        result: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ip = entry.get("ip")
            if isinstance(ip, str) and ip and ip not in result:
                result.append(ip)
        return tuple(result)


@dataclass(frozen=True)
class GuiAppSettings:
    """Preferences that belong to the Proton GUI process only.

    pno shows them read-only: auto-connect and tray pinning must stay explicit
    user decisions in the Proton app, and pno never connects on its own.
    """

    auto_connect: str | None = None
    start_minimized: bool = False
    pinned: tuple[str, ...] = ()
    available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "connect_at_app_startup": self.auto_connect,
            "start_app_minimized": self.start_minimized,
            "tray_pinned_servers": list(self.pinned),
            "available": self.available,
        }


@dataclass(frozen=True)
class ApplyResult:
    changed: bool
    applied: tuple[str, ...]
    warnings: tuple[str, ...]
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "changed": self.changed,
            "applied": list(self.applied),
            "warnings": list(self.warnings),
            "path": str(self.path),
        }


def settings_path(path: Path | None = None) -> Path:
    return path or paths.proton_settings_file()


def app_config_path(path: Path | None = None) -> Path:
    return path or paths.proton_app_config_file()


def load(path: Path | None = None) -> ProtonSettings:
    """Read the settings file without materialising Proton's defaults."""
    target = settings_path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProtonSettings(path=target, raw={}, exists=False)
    except OSError as exc:
        raise SettingsError(
            f"cannot read Proton VPN settings {target}",
            detail=str(exc),
            hint="Check the file permissions or run 'pno doctor'.",
        ) from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise SettingsError(
            f"Proton VPN settings are not valid JSON: {target}",
            detail=str(exc),
            hint="Repair the file with the Proton VPN GUI before editing it here.",
        ) from exc
    if not isinstance(raw, dict):
        raise SettingsError(
            f"Proton VPN settings are not a JSON object: {target}",
            hint="Repair the file with the Proton VPN GUI before editing it here.",
        )
    return ProtonSettings(path=target, raw=raw, exists=True)


def read_app_config(path: Path | None = None) -> GuiAppSettings:
    """Read the GUI-only app configuration; never raises for a missing file."""
    target = app_config_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GuiAppSettings()
    if not isinstance(raw, dict):
        return GuiAppSettings()
    pinned = raw.get("tray_pinned_servers")
    servers = (
        tuple(item for item in pinned if isinstance(item, str))
        if isinstance(pinned, list)
        else ()
    )
    auto_connect = raw.get("connect_at_app_startup")
    return GuiAppSettings(
        auto_connect=auto_connect if isinstance(auto_connect, str) else None,
        start_minimized=_as_bool(raw.get("start_app_minimized"), False),
        pinned=servers,
        available=True,
    )


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in BOOL_WORDS:
            return BOOL_WORDS[word]
    raise SettingsError(
        "expected 'on' or 'off'",
        detail=repr(value),
        hint="Use on/off (or true/false).",
    )


def parse_netshield(value: Any) -> str:
    if isinstance(value, str) and value.strip() in NETSHIELD_VALUES:
        return value.strip()
    raise SettingsError(
        "invalid NetShield mode",
        detail=repr(value),
        hint="Use off, malware-only or malware-ads-trackers.",
    )


def parse_killswitch(value: Any) -> str:
    if isinstance(value, str) and value.strip() in KILLSWITCH_VALUES:
        return value.strip()
    raise SettingsError(
        "invalid kill switch mode",
        detail=repr(value),
        hint="Use off or standard; Advanced kill switch is managed by the GUI.",
    )


def parse_protocol(
    value: Any,
    catalog: Sequence[ProtocolOption] | None = None,
) -> str:
    if not isinstance(value, str):
        raise SettingsError("invalid protocol", detail=repr(value))
    key = value.strip().lower()
    protocol = PROTOCOL_ALIASES.get(key, key)
    options = catalog or FALLBACK_PROTOCOLS
    known = {option.id for option in options}
    if protocol not in known:
        names = ", ".join(sorted(known))
        raise SettingsError(
            "unknown protocol",
            detail=str(value),
            hint=f"Available protocols: {names}.",
        )
    return protocol


def parse_dns_servers(values: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        parts = values.replace(",", "\n").splitlines()
    else:
        parts = list(values)
    result: list[str] = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        try:
            address = ipaddress.ip_address(text)
        except ValueError as exc:
            raise SettingsError(
                f"invalid DNS address: {text}",
                hint="Use IPv4 or IPv6 addresses separated by commas.",
            ) from exc
        rendered = address.compressed
        if rendered not in result:
            result.append(rendered)
    return tuple(result)


def parse_app_paths(values: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        parts = values.splitlines()
    else:
        parts = list(values)
    result: list[str] = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        if "\x00" in text or "\n" in text:
            raise SettingsError("invalid application path", detail=repr(text))
        if not text.startswith("/"):
            raise SettingsError(
                f"application path must be absolute: {text}",
                hint="Use a path such as /usr/bin/example.",
            )
        if text not in result:
            result.append(text)
    return tuple(result)


def apply_changes(
    changes: Mapping[str, Any],
    *,
    path: Path | None = None,
    connected: bool = False,
    gui_running: bool = False,
    tier: int | None = None,
    catalog: Sequence[ProtocolOption] | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Validate *changes* and persist them atomically when needed.

    ``changes`` uses the canonical field names: ``protocol``, ``netshield``,
    ``split_tunneling_apps``, and so on.
    """
    unknown = set(changes) - _WRITABLE_FIELDS
    if unknown:
        raise SettingsError("unsupported settings", detail=", ".join(sorted(unknown)))
    if gui_running:
        raise SettingsError(
            "the Proton VPN GUI is running",
            hint="Close the GUI before saving settings so it cannot overwrite them.",
        )
    if connected:
        blocked = sorted(DISCONNECTED_ONLY & set(changes))
        if blocked:
            raise SettingsError(
                "disconnect before changing these settings",
                detail=", ".join(blocked),
                hint="The Proton VPN GUI also locks them while a connection is active.",
            )

    target = settings_path(path)
    lock_path = paths.state_dir() / "proton-settings.lock"
    with exclusive_lock(lock_path):
        current = load(target)
        merged = copy.deepcopy(current.raw)
        warnings: list[str] = []
        paid_requested = _paid_requested(changes)
        if paid_requested and tier is not None and tier < 1:
            raise SettingsError(
                f"{paid_requested} requires a paid Proton VPN plan",
                hint="Upgrade the plan or leave the setting off.",
            )
        if paid_requested and tier is None:
            warnings.append(
                "the Proton plan is unknown; the paid setting was saved anyway"
            )
        _apply_to_raw(merged, changes, catalog=catalog, warnings=warnings)
        _resolve_conflicts(merged, changes, warnings=warnings)
        dns = _dict(merged.get("custom_dns"))
        if _as_bool(dns.get("enabled"), False) and not _dns_ips(merged):
            raise SettingsError(
                "custom DNS is enabled but no DNS servers are configured",
                hint="Pass --dns with at least one address.",
            )
        _check_split_tunneling(merged, warnings)

        changed = merged != current.raw
        if changed and not dry_run:
            _write(target, merged)
        return ApplyResult(
            changed=changed,
            applied=tuple(sorted(changes)),
            warnings=tuple(dict.fromkeys(warnings)),
            path=target,
        )


def apply_killswitch(
    value: str,
    *,
    path: Path | None = None,
    connected: bool = False,
    gui_running: bool = False,
    dry_run: bool = False,
    runner: Runner | None = None,
    timeout: float = 60.0,
) -> ApplyResult:
    """Change the kill switch through Proton's own CLI.

    The official CLI applies the firewall state immediately; writing the JSON
    alone would leave Advanced kill switch rules active until the next
    connection. Advanced mode itself stays GUI-only.
    """
    target_mode = parse_killswitch(value)
    if target_mode == "advanced":
        raise SettingsError(
            "advanced kill switch is managed by the Proton VPN GUI",
            hint="Enable it in the GUI; pno can switch it off or to standard.",
        )
    if gui_running:
        raise SettingsError(
            "the Proton VPN GUI is running",
            hint="Close the GUI before changing the kill switch.",
        )
    if connected:
        raise SettingsError(
            "disconnect before changing the kill switch",
            hint="The Proton VPN GUI also locks this setting while connected.",
        )
    current = load(path)
    if current.killswitch == target_mode:
        return ApplyResult(
            changed=False,
            applied=("killswitch",),
            warnings=(),
            path=current.path,
        )
    if dry_run:
        return ApplyResult(
            changed=True,
            applied=("killswitch",),
            warnings=(),
            path=current.path,
        )

    active_runner = runner or run
    result = active_runner(
        ["protonvpn", "config", "set", "kill-switch", target_mode],
        timeout=timeout,
        error_factory=SettingsError,
        error_detail="protonvpn config set kill-switch",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise SettingsError(
            "protonvpn could not change the kill switch",
            detail=detail or f"exit code {result.returncode}",
            hint="Make sure you are signed in and disconnected.",
        )
    updated = load(current.path)
    if updated.killswitch != target_mode:
        raise SettingsError(
            "the kill switch value did not change",
            detail=str(updated.path),
            hint="Check the Proton VPN CLI output and try again.",
        )
    return ApplyResult(
        changed=True,
        applied=("killswitch",),
        warnings=(),
        path=updated.path,
    )


# --------------------------------------------------------------------- helpers
_WRITABLE_FIELDS = {
    "protocol",
    "netshield",
    "port_forwarding",
    "split_tunneling_enabled",
    "split_tunneling_mode",
    "split_tunneling_apps",
    "vpn_accelerator",
    "moderate_nat",
    "ipv6",
    "custom_dns_enabled",
    "custom_dns_ips",
    "telemetry",
    "anonymous_crash_reports",
}


def _paid_requested(changes: Mapping[str, Any]) -> str:
    if "netshield" in changes and parse_netshield(changes["netshield"]) != "off":
        return "NetShield"
    if "port_forwarding" in changes and parse_bool(changes["port_forwarding"]):
        return "port forwarding"
    if any(
        key in changes
        for key in (
            "split_tunneling_enabled",
            "split_tunneling_mode",
            "split_tunneling_apps",
        )
    ):
        enabled = changes.get("split_tunneling_enabled")
        if enabled is None or parse_bool(enabled):
            return "split tunneling"
    if "custom_dns_enabled" in changes and parse_bool(changes["custom_dns_enabled"]):
        return "custom DNS"
    if "custom_dns_ips" in changes:
        return "custom DNS"
    return ""


def _apply_to_raw(
    raw: dict[str, Any],
    changes: Mapping[str, Any],
    *,
    catalog: Sequence[ProtocolOption] | None,
    warnings: list[str],
) -> None:
    # Fixed order: a mode change must be applied before the application list of
    # that mode, regardless of the caller's mapping order.
    order = (
        "protocol",
        "netshield",
        "port_forwarding",
        "vpn_accelerator",
        "moderate_nat",
        "ipv6",
        "split_tunneling_enabled",
        "split_tunneling_mode",
        "split_tunneling_apps",
        "custom_dns_enabled",
        "custom_dns_ips",
        "telemetry",
        "anonymous_crash_reports",
    )
    for key in order:
        if key not in changes:
            continue
        value = changes[key]
        if key == "protocol":
            raw["protocol"] = parse_protocol(value, catalog)
        elif key == "netshield":
            features = _ensure_dict(raw, "features")
            features["netshield"] = NETSHIELD_VALUES[parse_netshield(value)]
        elif key == "port_forwarding":
            _ensure_dict(raw, "features")["port_forwarding"] = parse_bool(value)
        elif key == "vpn_accelerator":
            _ensure_dict(raw, "features")["vpn_accelerator"] = parse_bool(value)
        elif key == "moderate_nat":
            _ensure_dict(raw, "features")["moderate_nat"] = parse_bool(value)
        elif key == "ipv6":
            raw["ipv6"] = parse_bool(value)
        elif key == "telemetry":
            raw["telemetry"] = parse_bool(value)
        elif key == "anonymous_crash_reports":
            raw["anonymous_crash_reports"] = parse_bool(value)
        elif key == "split_tunneling_enabled":
            _split_group(raw)["enabled"] = parse_bool(value)
            _ensure_split_configs(raw)
        elif key == "split_tunneling_mode":
            mode = str(value).strip().lower()
            if mode not in {"exclude", "include"}:
                raise SettingsError(
                    "invalid split tunneling mode",
                    detail=repr(value),
                    hint="Use exclude or include.",
                )
            _split_group(raw)["mode"] = mode
            _ensure_split_configs(raw)
        elif key == "split_tunneling_apps":
            apps = parse_app_paths(value)
            mode = _split_mode(raw)
            config = _ensure_split_configs(raw)[mode]
            config["app_paths"] = list(apps)
            for app in apps:
                if not Path(app).exists():
                    warnings.append(f"application path does not exist: {app}")
        elif key == "custom_dns_enabled":
            _ensure_dict(raw, "custom_dns")["enabled"] = parse_bool(value)
        elif key == "custom_dns_ips":
            ips = parse_dns_servers(value)
            _ensure_dict(raw, "custom_dns")["ip_list"] = [
                {"ip": ip, "enabled": True} for ip in ips
            ]
        else:  # pragma: no cover - guarded by _WRITABLE_FIELDS
            raise SettingsError("unsupported setting", detail=key)


def _resolve_conflicts(
    raw: dict[str, Any], changes: Mapping[str, Any], *, warnings: list[str]
) -> None:
    dns = _dict(raw.get("custom_dns"))
    netshield = _as_int(_dict(raw.get("features")).get("netshield"), DEFAULT_NETSHIELD)
    if netshield == 0 or not _as_bool(dns.get("enabled"), False):
        return
    changed_netshield = "netshield" in changes
    changed_dns = "custom_dns_enabled" in changes or "custom_dns_ips" in changes
    if changed_netshield and changed_dns:
        raise SettingsError(
            "NetShield and custom DNS cannot be enabled together",
            hint="Disable one of them and apply again.",
        )
    if changed_netshield:
        dns["enabled"] = False
        warnings.append("custom DNS was disabled because NetShield is on")
    elif changed_dns:
        _ensure_dict(raw, "features")["netshield"] = 0
        warnings.append("NetShield was disabled because custom DNS is on")


def _check_split_tunneling(raw: dict[str, Any], warnings: list[str]) -> None:
    group = _dict(_dict(raw.get("features")).get("split_tunneling"))
    if not _as_bool(group.get("enabled"), False):
        return
    killswitch = _as_int(raw.get("killswitch"), DEFAULT_KILLSWITCH)
    if killswitch != 0:
        warnings.append(
            "Proton does not apply split tunneling while the kill switch is on"
        )
    config = _split_config_view(raw, _split_mode(raw))
    if not config.get("app_paths") and not config.get("ip_ranges"):
        warnings.append("split tunneling is on but no applications are selected")


def _write(target: Path, raw: dict[str, Any]) -> None:
    try:
        mode = target.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    text = json.dumps(raw, ensure_ascii=False, indent=4)
    try:
        atomic_write_text(target, text, mode=mode)
    except OSError as exc:
        raise SettingsError(
            f"cannot write Proton VPN settings {target}",
            detail=str(exc),
            hint="Check the file permissions.",
        ) from exc


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ensure_dict(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        value = {}
        raw[key] = value
    return value


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _features_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    return _as_bool(_dict(raw.get("features")).get(key), default)


def _split_group(raw: dict[str, Any]) -> dict[str, Any]:
    return _ensure_dict(_ensure_dict(raw, "features"), "split_tunneling")


def _split_group_view(raw: dict[str, Any]) -> dict[str, Any]:
    return _dict(_dict(raw.get("features")).get("split_tunneling"))


def _split_mode(raw: dict[str, Any]) -> str:
    mode = _split_group_view(raw).get("mode")
    return mode if mode in {"exclude", "include"} else "exclude"


def _split_configs_view(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return both mode configs without modifying the loaded document."""
    group = _dict(_dict(raw.get("features")).get("split_tunneling"))
    stored = group.get("config_by_mode")
    if not isinstance(stored, dict):
        stored = {
            mode: group[mode]
            for mode in ("exclude", "include")
            if isinstance(group.get(mode), dict)
        }
    view: dict[str, dict[str, Any]] = {}
    for mode in ("exclude", "include"):
        config = stored.get(mode)
        if not isinstance(config, dict):
            config = {}
        apps = config.get("app_paths")
        ranges = config.get("ip_ranges")
        view[mode] = {
            **config,
            "app_paths": (
                [item for item in apps if isinstance(item, str)]
                if isinstance(apps, list)
                else []
            ),
            "ip_ranges": (
                [item for item in ranges if isinstance(item, str)]
                if isinstance(ranges, list)
                else []
            ),
        }
    return view


def _split_config_view(raw: dict[str, Any], mode: str) -> dict[str, Any]:
    return _split_configs_view(raw).get(mode, {})


def _ensure_split_configs(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    group = _split_group(raw)
    configs = group.get("config_by_mode")
    if not isinstance(configs, dict):
        configs = {}
        for mode in ("exclude", "include"):
            legacy = group.get(mode)
            if isinstance(legacy, dict):
                configs[mode] = copy.deepcopy(legacy)
        group["config_by_mode"] = configs
    for mode in ("exclude", "include"):
        config = configs.get(mode)
        if not isinstance(config, dict):
            config = {}
            configs[mode] = config
        config.setdefault("mode", mode)
        if not isinstance(config.get("app_paths"), list):
            config["app_paths"] = []
        if not isinstance(config.get("ip_ranges"), list):
            config["ip_ranges"] = []
    return configs


def _split_config(raw: dict[str, Any], mode: str) -> dict[str, Any]:
    return _split_config_view(raw, mode)


def _dns_ips(raw: dict[str, Any]) -> tuple[str, ...]:
    entries = _dict(raw.get("custom_dns")).get("ip_list")
    if not isinstance(entries, list):
        return ()
    result: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            ip = entry.get("ip")
            if isinstance(ip, str) and ip:
                result.append(ip)
    return tuple(result)
