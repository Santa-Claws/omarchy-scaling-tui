#!/usr/bin/env python3
"""omarchy-scaling-tui — edit HiDPI scale factors for Discord and Spotify."""

import curses
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections import defaultdict

HOME = Path.home()
STEP = 0.05
MIN_SCALE = 0.1
MAX_SCALE = 3.0

AUTOSTART_PATH    = HOME / ".config/hypr/autostart.conf"
BINDINGS_PATH     = HOME / ".config/hypr/bindings.conf"
DISCORD_DESK_PATH = HOME / ".local/share/applications/com.discordapp.Discord.desktop"
SPOTIFY_DESK_PATH = HOME / ".local/share/applications/spotify.desktop"

PAT_DISCORD_AUTOSTART = re.compile(
    r'(exec-once = \[workspace 3 silent\] flatpak run com\.discordapp\.Discord'
    r' --force-device-scale-factor=)([\d.]+)'
)
PAT_SPOTIFY_AUTOSTART = re.compile(
    r'(exec-once = \[workspace 4 silent\] uwsm-app -- spotify'
    r' --force-device-scale-factor=)([\d.]+)'
)
PAT_SPOTIFY_BINDING = re.compile(
    r'(bindd = SUPER SHIFT, M, Music, exec, omarchy-launch-or-focus spotify'
    r' \"uwsm-app -- spotify --force-device-scale-factor=)([\d.]+)(\")'
)
PAT_DISCORD_DESKTOP = re.compile(
    r'(Exec=.*com\.discordapp\.Discord.*--force-device-scale-factor=)([\d.]+)$',
    re.MULTILINE
)
PAT_SPOTIFY_DESKTOP = re.compile(
    r'(Exec=spotify --force-device-scale-factor=)([\d.]+)( --uri=%u)'
)


@dataclass
class FileSpec:
    path: Path
    pattern: re.Pattern
    has_trailing_group: bool = False


@dataclass
class AppConfig:
    name: str
    files: list
    canonical_idx: int = 0


@dataclass
class AppState:
    config: AppConfig
    scale: float = 0.7
    saved_scale: float = 0.7
    load_error: Optional[str] = None

    @property
    def dirty(self) -> bool:
        return abs(self.scale - self.saved_scale) > 1e-9

    @property
    def can_save(self) -> bool:
        return self.load_error is None


@dataclass
class UIState:
    selected: int = 0
    status_msg: str = ""
    status_kind: str = "info"
    quit_armed: bool = False


DISCORD_CFG = AppConfig(
    name="Discord",
    files=[
        FileSpec(AUTOSTART_PATH,    PAT_DISCORD_AUTOSTART, False),
        FileSpec(DISCORD_DESK_PATH, PAT_DISCORD_DESKTOP,   False),
    ],
)
SPOTIFY_CFG = AppConfig(
    name="Spotify",
    files=[
        FileSpec(AUTOSTART_PATH,   PAT_SPOTIFY_AUTOSTART, False),
        FileSpec(BINDINGS_PATH,    PAT_SPOTIFY_BINDING,   True),
        FileSpec(SPOTIFY_DESK_PATH, PAT_SPOTIFY_DESKTOP,  True),
    ],
)
APPS = [DISCORD_CFG, SPOTIFY_CFG]


def fmt_scale(v: float) -> str:
    v = round(round(v / STEP) * STEP, 10)
    s = f'{v:.10f}'.rstrip('0')
    if s.endswith('.'):
        s += '0'
    return s


def load_all(app_states: list) -> str:
    msgs = []
    for ast in app_states:
        fs = ast.config.files[ast.config.canonical_idx]
        try:
            content = fs.path.read_text()
            m = fs.pattern.search(content)
            if m:
                ast.scale = float(m.group(2))
                ast.saved_scale = ast.scale
                ast.load_error = None
            else:
                ast.load_error = "pattern not found"
                msgs.append(f"{ast.config.name}: pattern not found in {fs.path.name}")
        except FileNotFoundError:
            ast.load_error = "file missing"
            msgs.append(f"{ast.config.name}: {fs.path.name} not found")
        except PermissionError:
            ast.load_error = "permission denied"
            msgs.append(f"{ast.config.name}: permission denied")
    if msgs:
        return "Load errors: " + "; ".join(msgs)
    discord_s = fmt_scale(app_states[0].scale)
    spotify_s = fmt_scale(app_states[1].scale)
    return f"Loaded. Discord={discord_s}  Spotify={spotify_s}"


def save_all(app_states: list, ui: UIState) -> None:
    blocked = [a for a in app_states if not a.can_save]
    if blocked:
        names = ", ".join(a.config.name for a in blocked)
        ui.status_msg = f"Cannot save — load error for: {names}"
        ui.status_kind = "error"
        return

    file_ops: dict = defaultdict(list)
    for ast in app_states:
        new_val = fmt_scale(ast.scale)
        for fs in ast.config.files:
            file_ops[fs.path].append((fs.pattern, fs.has_trailing_group, new_val))

    errors = []
    for path, ops in file_ops.items():
        try:
            content = path.read_text()
            for pattern, has_trailing, new_val in ops:
                if has_trailing:
                    content = pattern.sub(r'\g<1>' + new_val + r'\g<3>', content)
                else:
                    content = pattern.sub(r'\g<1>' + new_val, content)
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            try:
                os.write(fd, content.encode())
                os.close(fd)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except PermissionError:
            errors.append(f"permission denied: {path.name}")
        except Exception as e:
            errors.append(f"{path.name}: {e}")

    if errors:
        ui.status_msg = "Save FAILED: " + "; ".join(errors)
        ui.status_kind = "error"
    else:
        for ast in app_states:
            ast.saved_scale = ast.scale
        d = fmt_scale(app_states[0].scale)
        s = fmt_scale(app_states[1].scale)
        ui.status_msg = f"Saved. Discord={d}  Spotify={s}"
        ui.status_kind = "ok"


def _safe_addstr(stdscr, y, x, text, attr=0):
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw(stdscr, app_states: list, ui: UIState) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    if h < 14 or w < 50:
        _safe_addstr(stdscr, 0, 0, "Terminal too small (need 50x14)")
        stdscr.refresh()
        return

    # Color pairs
    C_TITLE   = curses.color_pair(1)
    C_WARN    = curses.color_pair(2)
    C_WIDGET  = curses.color_pair(3)
    C_OK      = curses.color_pair(4)
    C_ERR     = curses.color_pair(5)
    C_TAB_SEL = curses.A_REVERSE

    title = " omarchy-scaling-tui — HiDPI Scale Factor Editor "
    _safe_addstr(stdscr, 0, max(0, (w - len(title)) // 2), title, C_TITLE | curses.A_BOLD)

    # Tabs
    tab_y = 2
    col = 3
    for i, ast in enumerate(app_states):
        label = f" {ast.config.name} "
        if ast.load_error:
            label = f" {ast.config.name} [ERR] "
        attr = C_TAB_SEL if i == ui.selected else 0
        _safe_addstr(stdscr, tab_y, col, label, attr)
        col += len(label) + 2

    hint = "  ←↑↓→ / TAB to switch"
    _safe_addstr(stdscr, tab_y, col, hint, curses.A_DIM)

    # Selected app detail
    ast = app_states[ui.selected]
    row = 4

    if ast.load_error:
        err_label = {"file missing": "[FILE MISSING]",
                     "pattern not found": "[PARSE ERROR]",
                     "permission denied": "[PERMISSION DENIED]"}.get(ast.load_error, "[ERROR]")
        _safe_addstr(stdscr, row, 3, f"  {err_label}", C_ERR | curses.A_BOLD)
        row += 1
    else:
        scale_str = fmt_scale(ast.scale)
        dirty_mark = " *" if ast.dirty else "  "
        _safe_addstr(stdscr, row, 3, f"  Scale:{dirty_mark} ")
        _safe_addstr(stdscr, row, 14, "[ - ]", C_WIDGET)
        _safe_addstr(stdscr, row, 20, f"  {scale_str}  ", curses.A_BOLD)
        _safe_addstr(stdscr, row, 20 + 2 + len(scale_str) + 2, "[ + ]", C_WIDGET)
        _safe_addstr(stdscr, row, 20 + 2 + len(scale_str) + 2 + 6,
                     "   ← / →  or  - / +  (step 0.05)", curses.A_DIM)
        row += 2

        _safe_addstr(stdscr, row, 3, "  Files that will be updated:", curses.A_DIM)
        row += 1
        for fs in ast.config.files:
            short = str(fs.path).replace(str(HOME), "~")
            _safe_addstr(stdscr, row, 5, f"  {short}", curses.A_DIM)
            row += 1

    # Help bar
    help_row = max(row + 1, h - 3)
    _safe_addstr(stdscr, help_row, 3, "  [s] Save   [r] Reload   [q] Quit")

    # Status bar
    bar_row = h - 1
    status_attr = {"ok": C_OK, "error": C_ERR, "warn": C_WARN}.get(ui.status_kind, 0)
    any_dirty = any(a.dirty for a in app_states)
    status_line = ui.status_msg
    if any_dirty:
        unsaved = " [unsaved]"
        _safe_addstr(stdscr, bar_row, 0, " " * (w - 1))
        _safe_addstr(stdscr, bar_row, 1, status_line[:w - len(unsaved) - 3], status_attr)
        _safe_addstr(stdscr, bar_row, w - len(unsaved) - 1, unsaved, C_WARN | curses.A_BOLD)
    else:
        _safe_addstr(stdscr, bar_row, 0, " " * (w - 1))
        _safe_addstr(stdscr, bar_row, 1, status_line[:w - 2], status_attr)

    stdscr.refresh()


def handle_key(key: int, app_states: list, ui: UIState) -> bool:
    n = len(app_states)
    ast = app_states[ui.selected]

    non_quit_key = True

    if key in (curses.KEY_UP, curses.KEY_DOWN, ord('\t')):
        if key == curses.KEY_UP:
            ui.selected = (ui.selected - 1) % n
        else:
            ui.selected = (ui.selected + 1) % n
        ui.status_msg = f"Selected: {app_states[ui.selected].config.name}"
        ui.status_kind = "info"

    elif key in (curses.KEY_LEFT, ord('-'), ord('_')):
        if ast.can_save:
            new = round((ast.scale - STEP) / STEP) * STEP
            new = max(MIN_SCALE, round(new, 10))
            ast.scale = new
            ui.status_msg = f"{ast.config.name}: scale → {fmt_scale(new)}"
            ui.status_kind = "info"

    elif key in (curses.KEY_RIGHT, ord('+'), ord('=')):
        if ast.can_save:
            new = round((ast.scale + STEP) / STEP) * STEP
            new = min(MAX_SCALE, round(new, 10))
            ast.scale = new
            ui.status_msg = f"{ast.config.name}: scale → {fmt_scale(new)}"
            ui.status_kind = "info"

    elif key in (ord('s'), ord('S')):
        save_all(app_states, ui)

    elif key in (ord('r'), ord('R')):
        msg = load_all(app_states)
        ui.status_msg = msg
        ui.status_kind = "error" if "error" in msg.lower() else "info"

    elif key in (ord('q'), ord('Q'), 27):
        non_quit_key = False
        any_dirty = any(a.dirty for a in app_states)
        if any_dirty and not ui.quit_armed:
            ui.status_msg = "Unsaved changes! Press [q] again to quit, [s] to save."
            ui.status_kind = "warn"
            ui.quit_armed = True
            return False
        return True

    if non_quit_key:
        ui.quit_armed = False

    return False


def main(stdscr) -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(1, curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_CYAN,   -1)
    curses.init_pair(4, curses.COLOR_GREEN,  -1)
    curses.init_pair(5, curses.COLOR_RED,    -1)

    app_states = [AppState(config=cfg) for cfg in APPS]
    ui = UIState()
    msg = load_all(app_states)
    ui.status_msg = msg
    ui.status_kind = "error" if "error" in msg.lower() else "info"

    while True:
        draw(stdscr, app_states, ui)
        key = stdscr.getch()
        if key == curses.KEY_RESIZE:
            continue
        if handle_key(key, app_states, ui):
            break


if __name__ == '__main__':
    curses.wrapper(main)
