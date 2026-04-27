# omarchy-scaling-tui

A terminal UI for adjusting `--force-device-scale-factor` for Discord and Spotify in an [Omarchy](https://omarchy.org) / Hyprland setup.

Edits all relevant config files atomically in one save:

| App | Files modified |
|-----|---------------|
| Discord | `~/.config/hypr/autostart.conf`, `~/.local/share/applications/com.discordapp.Discord.desktop` |
| Spotify | `~/.config/hypr/autostart.conf`, `~/.config/hypr/bindings.conf`, `~/.local/share/applications/spotify.desktop` |

## Install

```bash
bash install.sh
```

This symlinks `scaling_tui.py` into `~/.local/bin/omarchy-scaling-tui`.

## Run

```bash
omarchy-scaling-tui
# or directly:
python3 scaling_tui.py
```

## Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` / `Tab` | Switch between Discord / Spotify |
| `←` / `-` | Decrease scale by 0.05 |
| `→` / `+` / `=` | Increase scale by 0.05 |
| `s` | Save all files |
| `r` | Reload from disk |
| `q` | Quit (confirm if unsaved) |

Scale range: 0.1 – 3.0. Changes take effect on next app launch (or autostart on next login).

## Requirements

Python 3 (stdlib only — no pip installs needed).
