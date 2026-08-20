# omarchy-scaling-tui

A terminal UI for global and per-app HiDPI scaling on an [Omarchy](https://omarchy.org) / Hyprland desktop.

Supports Omarchy Quattro's active Lua configuration and automatically falls back to legacy `.conf` files on older installs.

Edits all relevant config files atomically in one save:

| Setting | Files modified |
|-----|---------------|
| Global scale | `~/.config/hypr/monitors.lua` (or legacy `monitors.conf`) |
| Electron apps | Desktop entry plus active Hyprland autostart/binding files when present |
| Discord Flatpak | Persistent wrapper config plus effective live Discord UI zoom |
| Native Flatpaks | Per-app `GDK_DPI_SCALE`, `GDK_SCALE`, or `QT_SCALE_FACTOR` override |

## Install

```bash
bash install.sh
```

This symlinks `scaling_tui.py` into `~/.local/bin/omarchy-scaling-tui` and installs a portable application-menu entry.

## Run

```bash
omarchy-scaling-tui
# or directly:
python3 scaling_tui.py
```

## Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` / `Tab` | Move through global and per-app settings |
| `←` / `-` | Decrease scale by 0.05 |
| `→` / `+` / `=` | Increase scale by 0.05 |
| `d` | Enable or disable the selected app override |
| `m` | Change the scaling method for native Flatpaks |
| `a` | Apply the selected value to active overrides |
| `/` | Search applications |
| `s` | Save all changes |
| `r` | Reload from disk |
| `q` | Quit (confirm if unsaved) |

Scale range: 0.1 – 3.0. Changes take effect on next app launch (or autostart on next login).
For a running Discord Flatpak, saving also applies its UI zoom immediately;
Chromium otherwise clamps command-line device scaling below 1.0.

## Requirements

Python 3 (stdlib only — no pip installs needed).
