# proton-net-optimizer (`pno`)

[![CI](https://github.com/Dude-BTW/proton-net-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/Dude-BTW/proton-net-optimizer/actions/workflows/ci.yml)

Suggests the least-loaded Proton VPN servers and keeps the Arch Linux
`mirrorlist` matched to the country your traffic actually exits from.

The tool **never switches the VPN by itself**. It measures, ranks and shows;
you press Connect.

- CLI and Textual TUI
- Country flags, search and an "inventory only" filter in both server views
- Secure Core: every route in the TUI, aggregates per exit country in the CLI
- Explicit Disconnect button and CLI command
- Single-instance TUI: the tray raises the existing window instead of opening a second one
- Remembers the active tab, search, filters and selected rows across restarts
- Detects a running Proton VPN GUI and offers to close it before connecting
- Mirror refresh on VPN exit change, on a schedule, and on demand
- Background daemon with pause/resume, persisted across reboots
- Tray icon and a KDE application-menu entry on Wayland
- Machine-readable JSONL event log with a query command
- Crash-safe: atomic writes, locks, timeouts, retries, no partial files
- Startup component checks in the TUI, tray and daemon

## Requirements

- Arch Linux, KDE Plasma 6, Wayland (X11 should work too)
- `proton-vpn-gtk-app` (provides the Proton Python libraries and session cache)
- `protonvpn` CLI (package `proton-vpn-cli`)
- `networkmanager` + `nmcli`
- Python 3.11+ (the project runs in its own `.venv`)
- `pyside6` for the tray icon (already present on most Plasma installs)
- `sudo` for automatic mirrorlist writes
- A terminal emulator for the tray/menu entry (Kitty, Konsole, Alacritty,
  Foot, WezTerm and XTerm are auto-detected)

`pno doctor` checks all of these explicitly; the TUI, tray and daemon also run
a lightweight check at startup.

## Install

```bash
cd ~/projects/python/proton_net_optimizer
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,tui]"

# One-time system integration:
#   - systemd user unit for the daemon
#   - autostart entry for the tray
#   - root-owned helper + /etc/sudoers.d/pno-mirror
.venv/bin/pno install
```

Besides the service and helper, this installs:
- `~/.local/share/applications/proton-net-optimizer.desktop` (KDE menu entry);
- `~/.local/share/icons/hicolor/scalable/apps/proton-net-optimizer.svg`.

`pno install` asks before touching sudo/systemd configuration. Use flags for a
non-interactive run:

```bash
pno install --yes                 # install everything
pno install --no-sudo             # skip automatic mirror writes
pno install --no-start            # do not start the daemon now
```

Uninstall (keeps config and logs by default):

```bash
pno uninstall          # remove unit, autostart and sudo helper
pno uninstall --purge  # also delete config and state/logs
```

## Usage

```bash
pno doctor                       # environment diagnostics
pno servers --top 10             # all countries, least-loaded servers first
pno servers --country UA,DE      # narrow the view
pno servers --secure-core        # aggregate Secure Core view
pno servers --secure-core --routes  # every Secure Core route
pno servers --inventory          # only countries/routes from the inventory
pno connect CH-US#3              # explicitly connect (you decide)
pno connect CH-US#3 --close-gui  # close a running Proton GUI, then connect
pno disconnect                   # disconnect now
pno mirrors status               # current target country and mirrorlist
pno mirrors preview              # measure, show diff, write nothing
pno mirrors refresh              # measure and apply
pno tui                          # full interface (current terminal)
pno launch                       # full interface in the configured terminal
pno tray                         # tray icon (autostarted by pno install)
pno daemon status|pause|resume|reload|shutdown
pno logs --level warning --since -2h
```

Every command that returns structured data also accepts `--json`.

### TUI

| Tab | Contents |
| --- | --- |
| Servers | All countries (best load first) and the top-N least-loaded servers in each, with flags, search and an inventory filter |
| Secure Core | All Secure Core routes with entry/exit flags, load, score, city and an inventory marker; search and inventory filter |
| Mirrors | Target country, last refresh, measured candidates, Preview / Refresh & apply, auto-refresh checkbox |
| Logs | Tail of the JSONL event stream |
| Settings | Top-N, home country, terminal, language (EN/UK) |

Keys: `F5` refresh, `c` connect to the selected row, `d` disconnect,
`p` pause/resume, `Ctrl+Q` quit. The current selection is never applied
automatically.

A line under the status bar always shows the connected server, its current
load, the least-loaded server in the same country (with the difference) and
the best alternative in the same pool (regular servers or Secure Core). Loads
update with the daemon's server refresh, so the decision to switch is based on
current measurements rather than the moment of connecting.

The status area uses the Catppuccin Mocha palette: muted labels, a blue exit
country, and a traffic-light load (green below 40%, yellow 40–69%, red 70% and
above). The Current line is a full-width dark band with equal one-line gaps
above and below; its colours live in `pno/tui/theme.py`.

On startup the TUI checks the runtime components (Arch/pacman, NetworkManager,
the Proton CLI and data source, mirror helper, terminal). Missing required
components open a dialog with hints; optional problems are shown as a warning.
The same check runs in the tray and the daemon and is recorded in the event log.

The TUI is single-instance. Opening it again from the tray, the KDE menu or
`pno launch` focuses the existing terminal window (via kitty's control socket,
then `kdotool`, `wmctrl` or `xdotool`). It never opens a second window; if the
window cannot be raised, a desktop notification says the TUI is already
running.

Session state (active tab, both search queries, inventory checkboxes and the
selected country/server/Secure Core route) is stored in
`~/.local/state/proton-net-optimizer/tui.json` and restored on the next launch.
It is written atomically and debounced, and flushed on exit.

Search is case-insensitive; several words are combined with AND. It matches
country codes and names (EN/UK), load values, server counts, server names and
cities. The “Inventory only” checkbox uses `pno/data/inventory.toml`:
`CH, DE, IS, JP, PL, SE, UA, US` for regular countries and the exact Secure
Core routes for the Secure Core tab.

If the Proton VPN GUI is running while a VPN connection is active, connecting
from the TUI opens a dialog. **Yes** closes the GUI, disconnects, and connects
through pno; **No** leaves everything untouched.

## How it works

### Server loads

The Proton CLI does not expose loads, and the REST endpoint requires a session.
Instead, `pno` runs `pno/proton/fetch_helper.py` with **`/usr/bin/python3`**,
which already has the Proton libraries installed by `proton-vpn-gtk-app`. The
helper returns a compact JSON list of all logical servers (name, entry/exit
country, city, load, score, tier, feature bits). No credentials are read or
stored by this project.

Fallbacks, in order:

1. helper (fresh loads, same session/cache as the GUI);
2. `~/.cache/Proton/VPN/serverlist.json` (flagged stale when loads expired);
3. the last compact cache in `$XDG_STATE_HOME/proton-net-optimizer/servers.json`.

Ranking is `load` ascending, then Proton's `score`, then name. Nothing is
hard-coded.

### Secure Core

The inventory (`pno/data/inventory.toml`) lists the Secure Core logical servers
the setup knows about, e.g. `CH-US#3` = entry Switzerland, exit United States.
The TUI lists all available routes (entry → exit, load, score, city) and marks
the inventory ones. The CLI keeps the aggregate per exit country: the
percentage is the lowest load among eligible routes for that country.

### Mirrors

1. Download `https://archlinux.org/mirrors/status/json/` (cached for an hour).
2. Keep `active` http/https mirrors with `completion_pct >= 0.95`, recent
   `last_sync`, and `delay <= 24h`. `rsync://` entries are never written:
   pacman cannot use them.
3. Candidate pool = mirrors in the target country + the globally best mirrors
   by Arch score.
4. Probe each candidate for real: a `Range` request for `core.db` from the
   current network, two attempts, short timeout.
5. Rank by measured latency (HTTPS preferred when within 15% of HTTP).
6. Write the top-N through the privileged helper, atomically, with a backup.
   If the resulting `Server =` lines are unchanged, the file is not touched.

The target country is the current VPN **exit** country. When disconnected, it
is `home_country` (default `UA`). Proton uses `UK` where Arch uses `GB`; the
mapping lives in `pno/data/countries.toml`.

### Daemon

A systemd user service (`pno-daemon.service`):

- polls NetworkManager every 10 s for `ProtonVPN <server>` connection names;
- refreshes loads every 60 s while running;
- after an exit-country change (15 s debounce) and every ~12 h ±10% jitter,
  measures and applies a new mirrorlist;
- enforces a single concurrent mirror operation;
- persists pause state, schedule, VPN snapshot and last errors in
  `state.json`;
- keeps serving status over a Unix socket in `$XDG_RUNTIME_DIR`.

`pno daemon pause` stops only automatic work; manual commands still run.

## Files

| Path | Purpose |
| --- | --- |
| `~/.config/proton-net-optimizer/config.toml` | configuration |
| `~/.config/proton-net-optimizer/inventory.toml` | optional inventory override |
| `~/.local/state/proton-net-optimizer/state.json` | shared state |
| `~/.local/state/proton-net-optimizer/servers.json` | compact server cache |
| `~/.local/state/proton-net-optimizer/mirrors_status.json` | Arch status cache |
| `~/.local/state/proton-net-optimizer/mirror_history.jsonl` | applied plans |
| `~/.local/state/proton-net-optimizer/tui.json` | last TUI tab, search, filters and selection |
| `~/.local/state/proton-net-optimizer/logs/events.jsonl` | machine log |
| `$XDG_RUNTIME_DIR/proton-net-optimizer/daemon.sock` | IPC socket (mode 0600) |
| `~/.local/share/applications/proton-net-optimizer.desktop` | KDE application-menu entry |
| `~/.local/share/icons/hicolor/scalable/apps/proton-net-optimizer.svg` | application icon |
| `/etc/pacman.d/mirrorlist` | generated mirrorlist |
| `/var/lib/pno/mirrorlist-*.bak` | previous mirrorlists (last 5) |

## Configuration

`config.toml` is flat; unknown keys are ignored.

| Key | Default | Meaning |
| --- | --- | --- |
| `top_n` | `10` | servers shown per country |
| `include_tor` | `false` | include Tor-only servers in suggestions |
| `home_country` | `"UA"` | mirror target when the VPN is off |
| `poll_interval` | `10` | VPN state poll, seconds |
| `loads_interval` | `60` | load refresh interval, seconds |
| `servers_max_age` | `120` | reuse cached loads younger than this |
| `auto_mirror` | `true` | enable scheduled/triggered mirror refresh |
| `auto_mirror_on_vpn_change` | `true` | refresh after the exit country changes |
| `mirror_debounce_seconds` | `15` | wait for the tunnel to settle |
| `mirror_interval_hours` | `12` | periodic refresh interval |
| `mirror_count` | `5` | mirrors written to the list |
| `mirror_global_candidates` | `20` | global candidates and per-country cap |
| `mirror_probe_timeout` | `5` | seconds per probe attempt |
| `mirror_probe_attempts` | `2` | probe attempts per mirror |
| `mirror_min_completion` | `0.95` | Arch completion threshold |
| `mirror_max_delay_hours` | `24` | maximum Arch sync delay |
| `mirror_max_age_hours` | `48` | maximum age of `last_sync` |
| `mirror_pacman_sync` | `false` | run `pacman -Sy` after applying |
| `mirrorlist_path` | `/etc/pacman.d/mirrorlist` | target file |
| `language` | `"en"` | `en` or `uk`; English is the default, changed only by the user |
| `terminal` | `""` | terminal used by “Open TUI”; empty means auto-detect |
| `log_level` | `"info"` | `debug`, `info`, `warning`, `error` |

## Logs

One JSON object per line:

```json
{"schema":1,"ts":"2026-11-21T18:00:00.123Z","run_id":"20261121T180000Z-a1b2c3",
 "seq":42,"pid":1234,"level":"info","component":"mirrors",
 "event":"mirrors.apply.done","data":{"country":"UA","servers":5},
 "duration_ms":1530,"error":null}
```

```bash
pno logs --level warning --since -24h --json
pno logs --event 'mirrors.*' --summary
pno logs --component vpn --limit 50
```

Rotated at 20 MB into `logs/archive/`. A torn last line (power loss) is
ignored by the reader.

## Reliability and security

- Every state/cache write is `tmp + fsync + rename`; mirror apply adds a
  cross-process `flock`.
- All subprocess and HTTP calls have timeouts; retries use backoff at the
  daemon level.
- Offline: operations fail with a typed error, the UI shows data age/staleness;
  the daemon keeps running and retries.
- The daemon survives crashes (`Restart=on-failure`); pause state and schedule are
  persisted.
- The TUI holds an `flock` in `$XDG_RUNTIME_DIR` and serves a focus socket, so
  concurrent launches cannot create duplicate windows.
- The root helper `/usr/local/lib/pno/pno-mirror-apply` is root-owned,
  standard-library-only, and validates every line. The sudoers rule allows
  exactly the two argument vectors used by `pno` (`--pacman-sync` or not).
- No credentials are stored; authentication stays inside Proton's own app.

## KDE menu

`pno install` registers **Proton Net Optimizer** under Network/Utilities and
installs the vector icon. The entry runs `pno launch`, which opens the TUI in
the terminal configured by the `terminal` key, or auto-detects Kitty, Konsole,
Alacritty, Foot, WezTerm or XTerm when the key is empty.

## Portability

- Only one user per machine should own automatic mirror updates, because the
  `mirrorlist` is system-wide. The user service, sudoers rule and backups are
  installed per user, but they all target the same system file.
- `home_country` defaults to `UA`; set it to your own country for the no-VPN
  case. The server inventory is an example that can be replaced with
  `~/.config/proton-net-optimizer/inventory.toml`.
- The terminal is auto-detected when `terminal` is empty; set it explicitly if
  the tray/menu should use another emulator.
- Configuration and state stay in XDG directories; no credentials are stored
  by this project.

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/black --check .
.venv/bin/isort --check-only .
.venv/bin/mypy pno
```

Tests are fully offline: no network, no Proton, no pacman, no sudo. The same
suite, lint and type checks run in GitHub Actions on every push and pull
request (`.github/workflows/ci.yml`, Arch Linux container).

## Proton VPN GUI

NetworkManager does not record which frontend created a connection. pno
therefore treats “GUI process running **and** VPN connected” as GUI ownership.
Before connecting from the TUI it offers to close the GUI (SIGTERM, SIGKILL
only as a fallback after 10 s), disconnect the old session, wait for
NetworkManager, and connect to the chosen server. Nothing happens unless you
press **Yes**.

## Known limitations

- The Proton fetch helper depends on private modules of the installed
  `proton-vpn-gtk-app`; a future update may require adjusting
  `pno/proton/fetch_helper.py`. `pno doctor` reports the problem and the cache
  fallback keeps the UI useful.
- Loads are as fresh as the last Proton refresh; the UI shows their age.
- Arch mirror scores are a prior, not a verdict; the measured latency decides.
- `pno` writes `mirrorlist` only; it does not upgrade packages.
