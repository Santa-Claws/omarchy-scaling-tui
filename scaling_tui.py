#!/usr/bin/env python3
"""omarchy-scaling-tui — global + per-app HiDPI scale editor for Hyprland."""

import curses
import os
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HOME         = Path.home()
MONITORS     = HOME / ".config/hypr/monitors.conf"
AUTOSTART    = HOME / ".config/hypr/autostart.conf"
BINDINGS     = HOME / ".config/hypr/bindings.conf"
APPS_DIR     = HOME / ".local/share/applications"

FLAG_RE      = re.compile(r'(--force-device-scale-factor=)([\d.]+)')
GDK_RE       = re.compile(r'^(env = GDK_SCALE,)([\d.]+)', re.MULTILINE)
MONITOR_RE   = re.compile(r'^(monitor\s*=\s*[^,]*,[^,]*,[^,]*,)(\d[\d.]*|auto)', re.MULTILINE)

SCALE_STEP   = 0.05
MON_STEP     = 0.25
MIN_APP      = 0.1
MAX_APP      = 3.0
MIN_MON      = 1.0
MAX_MON      = 4.0


# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class Occurrence:
    path: Path
    line: str   # exact original line content (used to locate and replace)


@dataclass
class AppOverride:
    name: str
    scale: float
    saved_scale: float
    occurrences: list   # list[Occurrence]
    load_error: Optional[str] = None

    @property
    def dirty(self):
        return abs(self.scale - self.saved_scale) > 1e-9


@dataclass
class GlobalSettings:
    gdk_scale: float           # value from env = GDK_SCALE,N  (0 = not found)
    monitor_scale: str         # "auto" or numeric string
    saved_gdk: float
    saved_mon: str
    load_error: Optional[str] = None

    @property
    def dirty(self):
        return (abs(self.gdk_scale - self.saved_gdk) > 1e-9 or
                self.monitor_scale != self.saved_mon)


@dataclass
class UIState:
    cursor: int = 0          # 0 = GDK row, 1 = monitor row, 2+ = app overrides
    status_msg: str = ""
    status_kind: str = "info"
    quit_armed: bool = False


# ── helpers ──────────────────────────────────────────────────────────────────

def fmt(v: float) -> str:
    v = round(round(v / SCALE_STEP) * SCALE_STEP, 10)
    s = f'{v:.10f}'.rstrip('0')
    return s + '0' if s.endswith('.') else s


def app_name_from_line(line: str) -> str:
    m = re.search(r'flatpak run\s+[\w.-]*\.(\w+)', line)
    if m:
        return m.group(1)
    m = re.search(r'omarchy-launch-or-focus\s+(\S+)', line)
    if m:
        return m.group(1).title()
    m = re.search(r'uwsm-app\s+--\s+(\S+)', line)
    if m:
        return m.group(1).title()
    m = re.search(r'exec\s*=\s*(\S+).*--force-device-scale-factor', line, re.IGNORECASE)
    if m:
        return Path(m.group(1)).stem.title()
    return "Unknown"


def desktop_name(path: Path) -> Optional[str]:
    try:
        for line in path.read_text().splitlines():
            if line.startswith('Name='):
                return line[5:].strip()
    except OSError:
        pass
    return None


def short(path: Path) -> str:
    return str(path).replace(str(HOME), '~')


def atomic_write(path: Path, content: str) -> None:
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


# ── discovery ────────────────────────────────────────────────────────────────

def discover_overrides() -> list:
    """Scan all config sources for --force-device-scale-factor, group by app."""
    apps: dict = {}   # lowercase_name -> AppOverride

    def _ingest(path: Path, lines: list, name_fn):
        for line in lines:
            m = FLAG_RE.search(line)
            if not m:
                continue
            scale = float(m.group(2))
            name = name_fn(line, path)
            if not name:
                continue
            key = name.lower()
            if key not in apps:
                apps[key] = AppOverride(
                    name=name, scale=scale,
                    saved_scale=scale, occurrences=[])
            else:
                # keep scale consistent with first seen; flag mismatch silently
                pass
            apps[key].occurrences.append(Occurrence(path=path, line=line))

    # autostart.conf
    try:
        lines = AUTOSTART.read_text().splitlines()
        _ingest(AUTOSTART, lines, lambda l, p: app_name_from_line(l))
    except OSError:
        pass

    # bindings.conf
    try:
        lines = BINDINGS.read_text().splitlines()
        _ingest(BINDINGS, lines, lambda l, p: app_name_from_line(l))
    except OSError:
        pass

    # .desktop files
    try:
        for desktop in sorted(APPS_DIR.glob('*.desktop')):
            try:
                text = desktop.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                if 'Exec' not in line:
                    continue
                if not FLAG_RE.search(line):
                    continue
                m = FLAG_RE.search(line)
                scale = float(m.group(2))
                name = desktop_name(desktop) or desktop.stem
                key = name.lower()
                if key not in apps:
                    apps[key] = AppOverride(
                        name=name, scale=scale,
                        saved_scale=scale, occurrences=[])
                else:
                    pass
                apps[key].occurrences.append(Occurrence(path=desktop, line=line))
    except OSError:
        pass

    return list(apps.values())


def load_global() -> GlobalSettings:
    try:
        text = MONITORS.read_text()
    except OSError as e:
        return GlobalSettings(0, 'auto', 0, 'auto', load_error=str(e))

    gdk = 0.0
    m = GDK_RE.search(text)
    if m:
        gdk = float(m.group(2))

    mon = 'auto'
    m = MONITOR_RE.search(text)
    if m:
        mon = m.group(2)

    err = None
    if gdk == 0:
        err = "GDK_SCALE not found in monitors.conf"

    return GlobalSettings(gdk, mon, gdk, mon, load_error=err)


# ── save ─────────────────────────────────────────────────────────────────────

def save_global(gs: GlobalSettings) -> Optional[str]:
    try:
        text = MONITORS.read_text()
    except OSError as e:
        return str(e)

    if gs.gdk_scale > 0:
        new_gdk = f'{gs.gdk_scale:.10f}'.rstrip('0').rstrip('.')
        text = GDK_RE.sub(r'\g<1>' + new_gdk, text)

    new_mon = gs.monitor_scale
    text = MONITOR_RE.sub(r'\g<1>' + new_mon, text)

    try:
        atomic_write(MONITORS, text)
        gs.saved_gdk = gs.gdk_scale
        gs.saved_mon = gs.monitor_scale
        return None
    except OSError as e:
        return str(e)


def save_override(ov: AppOverride) -> Optional[str]:
    new_val = fmt(ov.scale)
    by_file: dict = defaultdict(list)
    for occ in ov.occurrences:
        by_file[occ.path].append(occ)

    errors = []
    for path, occs in by_file.items():
        try:
            content = path.read_text()
            for occ in occs:
                new_line = FLAG_RE.sub(r'\g<1>' + new_val, occ.line)
                content = content.replace(occ.line, new_line, 1)
                occ.line = new_line   # update so subsequent saves work
            atomic_write(path, content)
        except OSError as e:
            errors.append(f"{path.name}: {e}")

    if errors:
        return "; ".join(errors)
    ov.saved_scale = ov.scale
    return None


def remove_override(ov: AppOverride) -> Optional[str]:
    """Strip --force-device-scale-factor from all occurrence lines."""
    by_file: dict = defaultdict(list)
    for occ in ov.occurrences:
        by_file[occ.path].append(occ)

    errors = []
    for path, occs in by_file.items():
        try:
            content = path.read_text()
            for occ in occs:
                new_line = FLAG_RE.sub('', occ.line).rstrip()
                content = content.replace(occ.line, new_line, 1)
            atomic_write(path, content)
        except OSError as e:
            errors.append(f"{path.name}: {e}")

    return "; ".join(errors) if errors else None


def save_all(gs: GlobalSettings, overrides: list, ui: UIState) -> None:
    errors = []
    e = save_global(gs)
    if e:
        errors.append(f"global: {e}")
    for ov in overrides:
        if ov.dirty:
            e = save_override(ov)
            if e:
                errors.append(f"{ov.name}: {e}")

    if errors:
        ui.status_msg = "Save FAILED: " + "; ".join(errors)
        ui.status_kind = "error"
    else:
        n = len(overrides)
        ui.status_msg = f"Saved. GDK_SCALE={int(gs.gdk_scale)}  {n} override{'s' if n != 1 else ''}"
        ui.status_kind = "ok"


# ── drawing ──────────────────────────────────────────────────────────────────

def _put(scr, y, x, text, attr=0):
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw(scr, gs: GlobalSettings, overrides: list, ui: UIState) -> None:
    scr.erase()
    h, w = scr.getmaxyx()

    if h < 12 or w < 52:
        _put(scr, 0, 0, "Terminal too small (need 52x12)")
        scr.refresh()
        return

    C_TITLE = curses.color_pair(1)
    C_WARN  = curses.color_pair(2)
    C_CTRL  = curses.color_pair(3)
    C_OK    = curses.color_pair(4)
    C_ERR   = curses.color_pair(5)
    C_DIM   = curses.A_DIM
    C_SEL   = curses.A_REVERSE

    title = " omarchy-scaling-tui — HiDPI Scale Factor Editor "
    _put(scr, 0, max(0, (w - len(title)) // 2), title, C_TITLE | curses.A_BOLD)

    row = 2

    # ── Global section ──
    _put(scr, row, 2, "Global", curses.A_BOLD)
    _put(scr, row, 9, " (monitors.conf)", C_DIM)
    row += 1
    _put(scr, row, 2, "─" * min(w - 4, 50), C_DIM)
    row += 1

    def draw_scale_row(y, label, value_str, idx, err=None):
        sel = (ui.cursor == idx)
        base = C_SEL if sel else 0
        pad = f"  {label:<18}"
        _put(scr, y, 2, pad, base)
        if err:
            _put(scr, y, 2 + len(pad), f"[{err}]", C_ERR)
        else:
            _put(scr, y, 2 + len(pad), "[ - ]", C_CTRL if sel else C_DIM)
            val_s = f"  {value_str}  "
            _put(scr, y, 2 + len(pad) + 5, val_s, curses.A_BOLD if sel else 0)
            _put(scr, y, 2 + len(pad) + 5 + len(val_s), "[ + ]", C_CTRL if sel else C_DIM)

    gdk_str = str(int(gs.gdk_scale)) if gs.gdk_scale > 0 else "?"
    draw_scale_row(row, "GDK_SCALE:", gdk_str, 0,
                   gs.load_error if gs.gdk_scale == 0 else None)
    row += 1
    draw_scale_row(row, "Monitor Scale:", gs.monitor_scale, 1)
    row += 2

    # ── Per-app section ──
    _put(scr, row, 2, "Per-App Overrides", curses.A_BOLD)
    _put(scr, row, 20, " (override global)", C_DIM)
    row += 1
    _put(scr, row, 2, "─" * min(w - 4, 50), C_DIM)
    row += 1

    if not overrides:
        _put(scr, row, 4, "No overrides found.", C_DIM)
        row += 1
    else:
        for i, ov in enumerate(overrides):
            idx = 2 + i
            sel = (ui.cursor == idx)
            base = C_SEL if sel else 0
            dirty = " *" if ov.dirty else "  "
            label = f"  {ov.name}{dirty}  "
            _put(scr, row, 2, f"{label:<22}", base)
            col = 2 + 22
            _put(scr, row, col, "[ - ]", C_CTRL if sel else C_DIM)
            val_s = f"  {fmt(ov.scale)}  "
            _put(scr, row, col + 5, val_s, curses.A_BOLD if sel else 0)
            col2 = col + 5 + len(val_s)
            _put(scr, row, col2, "[ + ]", C_CTRL if sel else C_DIM)
            # file hints
            file_hints = []
            seen = set()
            for occ in ov.occurrences:
                tag = occ.path.stem[:8] if occ.path.suffix == '.desktop' else occ.path.stem
                if tag not in seen:
                    file_hints.append(tag)
                    seen.add(tag)
            _put(scr, row, col2 + 6, "  " + " • ".join(file_hints), C_DIM)
            row += 1

    # ── help ──
    help_row = max(row + 1, h - 3)
    any_dirty = gs.dirty or any(o.dirty for o in overrides)
    help = "  [s] Save  [r] Reload  [d] Remove override  [q] Quit"
    _put(scr, help_row, 0, help, C_DIM)

    # ── status bar ──
    bar = h - 1
    s_attr = {"ok": C_OK, "error": C_ERR, "warn": C_WARN}.get(ui.status_kind, 0)
    _put(scr, bar, 0, " " * (w - 1))
    _put(scr, bar, 1, ui.status_msg[:w - 12], s_attr)
    if any_dirty:
        tag = "[unsaved]"
        _put(scr, bar, w - len(tag) - 1, tag, C_WARN | curses.A_BOLD)

    scr.refresh()


# ── input ────────────────────────────────────────────────────────────────────

def handle_key(key: int, gs: GlobalSettings, overrides: list,
               ui: UIState) -> bool:
    total = 2 + len(overrides)
    non_quit = True

    if key in (curses.KEY_UP,):
        ui.cursor = (ui.cursor - 1) % total
    elif key in (curses.KEY_DOWN, ord('\t')):
        ui.cursor = (ui.cursor + 1) % total

    elif key in (curses.KEY_LEFT, ord('-'), ord('_')):
        _adjust(gs, overrides, ui, -1)

    elif key in (curses.KEY_RIGHT, ord('+'), ord('=')):
        _adjust(gs, overrides, ui, +1)

    elif key in (ord('s'), ord('S')):
        save_all(gs, overrides, ui)

    elif key in (ord('r'), ord('R')):
        _reload(gs, overrides, ui)

    elif key in (ord('d'), ord('D')):
        _remove(overrides, ui)

    elif key in (ord('q'), ord('Q'), 27):
        non_quit = False
        any_dirty = gs.dirty or any(o.dirty for o in overrides)
        if any_dirty and not ui.quit_armed:
            ui.status_msg = "Unsaved changes! Press [q] again to quit, [s] to save."
            ui.status_kind = "warn"
            ui.quit_armed = True
            return False
        return True

    if non_quit:
        ui.quit_armed = False
    return False


def _adjust(gs: GlobalSettings, overrides: list, ui: UIState, direction: int):
    c = ui.cursor
    if c == 0:                  # GDK_SCALE
        new = max(1, min(8, int(gs.gdk_scale) + direction))
        gs.gdk_scale = float(new)
        ui.status_msg = f"GDK_SCALE → {new}"
        ui.status_kind = "info"
    elif c == 1:                # monitor scale
        if gs.monitor_scale == 'auto':
            if direction > 0:
                gs.monitor_scale = fmt_mon(MIN_MON)
        else:
            cur = float(gs.monitor_scale)
            new = cur + direction * MON_STEP
            if new < MIN_MON:
                gs.monitor_scale = 'auto'
            else:
                gs.monitor_scale = fmt_mon(min(MAX_MON, new))
        ui.status_msg = f"Monitor scale → {gs.monitor_scale}"
        ui.status_kind = "info"
    else:                       # app override
        idx = c - 2
        if idx < len(overrides):
            ov = overrides[idx]
            new = round((ov.scale + direction * SCALE_STEP) / SCALE_STEP) * SCALE_STEP
            ov.scale = max(MIN_APP, min(MAX_APP, round(new, 10)))
            ui.status_msg = f"{ov.name} → {fmt(ov.scale)}"
            ui.status_kind = "info"


def fmt_mon(v: float) -> str:
    s = f'{v:.4f}'.rstrip('0')
    return s + '0' if s.endswith('.') else s


def _reload(gs: GlobalSettings, overrides: list, ui: UIState):
    new_gs = load_global()
    gs.__dict__.update(new_gs.__dict__)
    new_ovs = discover_overrides()
    overrides.clear()
    overrides.extend(new_ovs)
    n = len(overrides)
    ui.status_msg = f"Reloaded. GDK_SCALE={int(gs.gdk_scale)}  {n} override{'s' if n != 1 else ''}"
    ui.status_kind = "info"
    ui.cursor = min(ui.cursor, max(0, 1 + n))


def _remove(overrides: list, ui: UIState):
    idx = ui.cursor - 2
    if idx < 0 or idx >= len(overrides):
        ui.status_msg = "Select a per-app override to remove."
        ui.status_kind = "warn"
        return
    ov = overrides[idx]
    err = remove_override(ov)
    if err:
        ui.status_msg = f"Remove failed: {err}"
        ui.status_kind = "error"
    else:
        overrides.pop(idx)
        ui.cursor = min(ui.cursor, 1 + len(overrides))
        ui.status_msg = f"Removed override for {ov.name}."
        ui.status_kind = "ok"


# ── main ─────────────────────────────────────────────────────────────────────

def main(scr) -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_CYAN,   -1)
    curses.init_pair(4, curses.COLOR_GREEN,  -1)
    curses.init_pair(5, curses.COLOR_RED,    -1)

    gs        = load_global()
    overrides = discover_overrides()
    ui        = UIState()

    n = len(overrides)
    if gs.load_error and gs.gdk_scale == 0:
        ui.status_msg  = f"Warning: {gs.load_error}"
        ui.status_kind = "warn"
    else:
        ui.status_msg  = f"Loaded. GDK_SCALE={int(gs.gdk_scale)}  {n} override{'s' if n != 1 else ''}"
        ui.status_kind = "info"

    while True:
        draw(scr, gs, overrides, ui)
        key = scr.getch()
        if key == curses.KEY_RESIZE:
            continue
        if handle_key(key, gs, overrides, ui):
            break


if __name__ == '__main__':
    curses.wrapper(main)
