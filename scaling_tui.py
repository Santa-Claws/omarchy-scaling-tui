#!/usr/bin/env python3
"""omarchy-scaling-tui — global + per-app HiDPI scale editor for Hyprland."""

import curses
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HOME      = Path.home()
MONITORS  = HOME / ".config/hypr/monitors.conf"
AUTOSTART = HOME / ".config/hypr/autostart.conf"
BINDINGS  = HOME / ".config/hypr/bindings.conf"


def _app_dirs() -> list:
    """All directories to scan for .desktop files, user-level first."""
    dirs = []
    xdg_home = os.environ.get('XDG_DATA_HOME', str(HOME / '.local/share'))
    dirs.append(Path(xdg_home) / 'applications')
    dirs.append(HOME / '.local/share/flatpak/exports/share/applications')
    dirs.append(Path('/var/lib/flatpak/exports/share/applications'))
    for d in os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':'):
        dirs.append(Path(d) / 'applications')
    return [d for d in dirs if d.is_dir()]

FLAG_RE    = re.compile(r'(--force-device-scale-factor=)([\d.]+)')
GDK_RE     = re.compile(r'^(env = GDK_SCALE,)([\d.]+)', re.MULTILINE)
MONITOR_RE = re.compile(r'^(monitor\s*=\s*[^,]*,[^,]*,[^,]*,)(\d[\d.]*|auto)', re.MULTILINE)
WEBAPP_RE  = re.compile(r'^omarchy-(?:launch-webapp|webapp-handler)')
EXEC_RE    = re.compile(r'^(Exec=)(.+)$', re.MULTILINE)

SCALE_STEP    = 0.05
MON_STEP      = 0.25
MIN_APP       = 0.1
MAX_APP       = 3.0
MIN_MON       = 1.0
MAX_MON       = 4.0
DEFAULT_SCALE = 0.7
GLOBAL_ROWS   = 3   # GDK_SCALE, Monitor Scale, Apply to All


# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class Occurrence:
    path: Path
    line: str


@dataclass
class AppEntry:
    name: str
    desktop_path: Optional[Path]
    scale: float
    has_override: bool
    saved_scale: float
    saved_has_override: bool
    occurrences: list   # list[Occurrence]

    @property
    def dirty(self):
        if self.has_override != self.saved_has_override:
            return True
        return self.has_override and abs(self.scale - self.saved_scale) > 1e-9


@dataclass
class GlobalSettings:
    gdk_scale: float
    monitor_scale: str
    saved_gdk: float
    saved_mon: str
    load_error: Optional[str] = None

    @property
    def dirty(self):
        return (abs(self.gdk_scale - self.saved_gdk) > 1e-9 or
                self.monitor_scale != self.saved_mon)


@dataclass
class UIState:
    cursor: int = 0
    scroll: int = 0
    apply_all: float = DEFAULT_SCALE
    search_mode: bool = False
    search_query: str = ""
    status_msg: str = ""
    status_kind: str = "info"
    quit_armed: bool = False


# ── helpers ──────────────────────────────────────────────────────────────────

def fmt(v: float) -> str:
    v = round(round(v / SCALE_STEP) * SCALE_STEP, 10)
    s = f'{v:.10f}'.rstrip('0')
    return s + '0' if s.endswith('.') else s


def fmt_mon(v: float) -> str:
    v = round(round(v / MON_STEP) * MON_STEP, 10)
    s = f'{v:.10f}'.rstrip('0')
    return s + '0' if s.endswith('.') else s


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


def app_name_from_line(line: str) -> Optional[str]:
    m = re.search(r'flatpak run\s+[\w.-]*\.(\w+)', line)
    if m:
        return m.group(1)
    m = re.search(r'omarchy-launch-or-focus\s+(\S+)', line)
    if m:
        return m.group(1).title()
    m = re.search(r'uwsm-app\s+--\s+(\S+)', line)
    if m:
        return m.group(1).title()
    return None


def _put(scr, y, x, text, attr=0):
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass


# ── discovery ────────────────────────────────────────────────────────────────

def parse_desktop(path: Path) -> Optional[dict]:
    """Parse [Desktop Entry] section; return None if hidden or webapp."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None

    in_entry = False
    name = exec_line = None
    no_display = False

    for line in lines:
        if line.strip() == '[Desktop Entry]':
            in_entry = True
            continue
        if in_entry and line.startswith('['):
            break
        if not in_entry:
            continue
        if line.startswith('Name=') and name is None:
            name = line[5:].strip()
        elif line.startswith('Exec=') and exec_line is None:
            exec_line = line
        elif line.strip() in ('NoDisplay=true', 'Hidden=true'):
            no_display = True

    if not name or not exec_line or no_display:
        return None

    exec_val = exec_line[5:].strip()
    if WEBAPP_RE.match(exec_val):
        return None

    return {'name': name, 'exec_line': exec_line}


def discover_apps() -> list:
    apps = {}        # name.lower() -> AppEntry
    seen_files: set = set()   # deduplicate by filename; user-level dirs come first

    # Step 1: all visible, non-webapp .desktop files across all XDG data dirs
    desktops = []
    for app_dir in _app_dirs():
        try:
            for p in sorted(app_dir.glob('*.desktop')):
                if p.name not in seen_files:
                    seen_files.add(p.name)
                    desktops.append(p)
        except OSError:
            continue

    for desktop in desktops:
        info = parse_desktop(desktop)
        if not info:
            continue
        name, exec_line = info['name'], info['exec_line']
        key = name.lower()
        m = FLAG_RE.search(exec_line)
        if m:
            scale = float(m.group(2))
            apps[key] = AppEntry(name=name, desktop_path=desktop,
                                 scale=scale, has_override=True,
                                 saved_scale=scale, saved_has_override=True,
                                 occurrences=[Occurrence(desktop, exec_line)])
        else:
            apps[key] = AppEntry(name=name, desktop_path=desktop,
                                 scale=DEFAULT_SCALE, has_override=False,
                                 saved_scale=DEFAULT_SCALE, saved_has_override=False,
                                 occurrences=[])

    # Step 2: autostart.conf — merge occurrences
    try:
        for line in AUTOSTART.read_text().splitlines():
            if not FLAG_RE.search(line):
                continue
            name = app_name_from_line(line)
            if not name:
                continue
            key = name.lower()
            scale = float(FLAG_RE.search(line).group(2))
            if key in apps:
                apps[key].occurrences.append(Occurrence(AUTOSTART, line))
                if not apps[key].has_override:
                    apps[key].scale = apps[key].saved_scale = scale
                    apps[key].has_override = apps[key].saved_has_override = True
            else:
                apps[key] = AppEntry(name=name, desktop_path=None,
                                     scale=scale, has_override=True,
                                     saved_scale=scale, saved_has_override=True,
                                     occurrences=[Occurrence(AUTOSTART, line)])
    except OSError:
        pass

    # Step 3: bindings.conf — merge occurrences
    try:
        for line in BINDINGS.read_text().splitlines():
            if not FLAG_RE.search(line):
                continue
            name = app_name_from_line(line)
            if not name:
                continue
            key = name.lower()
            if key in apps:
                apps[key].occurrences.append(Occurrence(BINDINGS, line))
    except OSError:
        pass

    # Sort: overrides first, then alphabetically
    entries = list(apps.values())
    entries.sort(key=lambda a: (not a.has_override, a.name.lower()))
    return entries


def load_global() -> GlobalSettings:
    try:
        text = MONITORS.read_text()
    except OSError as e:
        return GlobalSettings(2.0, 'auto', 2.0, 'auto', load_error=str(e))

    gdk = 2.0
    m = GDK_RE.search(text)
    if m:
        gdk = float(m.group(2))

    mon = 'auto'
    m = MONITOR_RE.search(text)
    if m:
        mon = m.group(2)

    err = None if GDK_RE.search(text) else "GDK_SCALE line not found in monitors.conf"
    return GlobalSettings(gdk, mon, gdk, mon, load_error=err)


# ── save ─────────────────────────────────────────────────────────────────────

def save_global(gs: GlobalSettings) -> Optional[str]:
    try:
        text = MONITORS.read_text()
    except OSError as e:
        return str(e)

    gdk_str = f'{gs.gdk_scale:.10f}'.rstrip('0').rstrip('.')
    if GDK_RE.search(text):
        text = GDK_RE.sub(r'\g<1>' + gdk_str, text)

    text = MONITOR_RE.sub(r'\g<1>' + gs.monitor_scale, text)

    try:
        atomic_write(MONITORS, text)
        gs.saved_gdk = gs.gdk_scale
        gs.saved_mon = gs.monitor_scale
        return None
    except OSError as e:
        return str(e)


def save_app(app: AppEntry) -> Optional[str]:
    errors = []
    new_val = fmt(app.scale)

    if app.has_override:
        if app.saved_has_override and app.occurrences:
            # Update existing flag in all occurrence files
            by_file: dict = defaultdict(list)
            for occ in app.occurrences:
                by_file[occ.path].append(occ)
            for path, occs in by_file.items():
                try:
                    content = path.read_text()
                    for occ in occs:
                        new_line = FLAG_RE.sub(r'\g<1>' + new_val, occ.line)
                        content = content.replace(occ.line, new_line, 1)
                        occ.line = new_line
                    atomic_write(path, content)
                except OSError as e:
                    errors.append(f"{path.name}: {e}")
        else:
            # Add flag to .desktop Exec line
            if not app.desktop_path:
                errors.append(f"{app.name}: no .desktop file to write override to")
            else:
                try:
                    content = app.desktop_path.read_text()

                    def _inject(m):
                        val = m.group(2)
                        # Insert before any %X args
                        sub = re.search(r'(\s+%\S+.*)$', val)
                        if sub:
                            return m.group(1) + val[:sub.start()] + \
                                   f' --force-device-scale-factor={new_val}' + val[sub.start():]
                        return m.group(1) + val + f' --force-device-scale-factor={new_val}'

                    new_content = EXEC_RE.sub(_inject, content, count=1)
                    atomic_write(app.desktop_path, new_content)
                    # Rebuild occurrence from the written line
                    for line in new_content.splitlines():
                        if line.startswith('Exec=') and FLAG_RE.search(line):
                            app.occurrences = [Occurrence(app.desktop_path, line)]
                            break
                except OSError as e:
                    errors.append(f"{app.desktop_path.name}: {e}")
    else:
        # Remove flag from all occurrence files
        by_file = defaultdict(list)
        for occ in app.occurrences:
            by_file[occ.path].append(occ)
        for path, occs in by_file.items():
            try:
                content = path.read_text()
                for occ in occs:
                    new_line = FLAG_RE.sub('', occ.line).rstrip()
                    content = content.replace(occ.line, new_line, 1)
                atomic_write(path, content)
            except OSError as e:
                errors.append(f"{path.name}: {e}")
        app.occurrences.clear()

    if not errors:
        app.saved_scale = app.scale
        app.saved_has_override = app.has_override
    return "; ".join(errors) if errors else None


def save_all(gs: GlobalSettings, apps: list, ui: UIState) -> None:
    errors = []
    e = save_global(gs)
    if e:
        errors.append(f"monitors.conf: {e}")
    for app in apps:
        if app.dirty:
            e = save_app(app)
            if e:
                errors.append(e)
    n_ovr = sum(1 for a in apps if a.has_override)
    if errors:
        ui.status_msg = "Save FAILED: " + "; ".join(errors)
        ui.status_kind = "error"
    else:
        ui.status_msg = (f"Saved. GDK_SCALE={int(gs.gdk_scale)}  "
                         f"{n_ovr} override{'s' if n_ovr != 1 else ''}  "
                         f"{len(apps)} apps")
        ui.status_kind = "ok"


# ── drawing ──────────────────────────────────────────────────────────────────

def _filter_apps(apps: list, query: str) -> list:
    if not query:
        return apps
    q = query.lower()
    return [a for a in apps if q in a.name.lower()]


def _put_name_highlighted(scr, row, col, name: str, query: str, base_attr, hi_attr):
    """Write app name, highlighting the matched substring."""
    if not query:
        _put(scr, row, col, name, base_attr)
        return
    idx = name.lower().find(query.lower())
    if idx < 0:
        _put(scr, row, col, name, base_attr)
        return
    end = idx + len(query)
    if idx > 0:
        _put(scr, row, col, name[:idx], base_attr)
    _put(scr, row, col + idx, name[idx:end], hi_attr)
    if end < len(name):
        _put(scr, row, col + end, name[end:], base_attr)


def _max_app_rows(h: int) -> int:
    # title(1) blank(1) global-hdr(1) sep(1) 3-global-rows(3) blank(1)
    # per-app-hdr(1) sep(1) help(1) status(1) = 12 overhead rows
    return max(1, h - 12)


def draw(scr, gs: GlobalSettings, apps: list, ui: UIState) -> None:
    scr.erase()
    h, w = scr.getmaxyx()

    if h < 14 or w < 54:
        _put(scr, 0, 0, "Terminal too small (need 54x14)")
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

    # ── Global section ──────────────────────────────────────────────────────
    _put(scr, row, 2, "Global", curses.A_BOLD)
    _put(scr, row, 9, " (monitors.conf)", C_DIM)
    row += 1
    _put(scr, row, 2, "─" * min(w - 4, 56), C_DIM)
    row += 1

    def draw_ctrl_row(y, label, value_str, idx, hint="", err=None):
        sel = (ui.cursor == idx)
        lbl = f"  {label:<20}"
        _put(scr, y, 2, lbl, C_SEL if sel else 0)
        col = 2 + len(lbl)
        if err:
            _put(scr, y, col, f"[{err}]", C_ERR)
        else:
            _put(scr, y, col, "[ - ]", C_CTRL if sel else C_DIM)
            val_s = f"  {value_str}  "
            _put(scr, y, col + 5, val_s, curses.A_BOLD if sel else 0)
            _put(scr, y, col + 5 + len(val_s), "[ + ]", C_CTRL if sel else C_DIM)
            if hint:
                _put(scr, y, col + 5 + len(val_s) + 6, hint, C_DIM)

    gdk_str = str(int(gs.gdk_scale)) if gs.gdk_scale > 0 else "?"
    draw_ctrl_row(row, "GDK_SCALE:", gdk_str, 0,
                  err=gs.load_error if gs.gdk_scale == 0 else None)
    row += 1
    draw_ctrl_row(row, "Monitor Scale:", gs.monitor_scale, 1, "step 0.25  auto below 1.0")
    row += 1
    draw_ctrl_row(row, "Apply to All:", fmt(ui.apply_all), 2,
                  "[a] sets all overrides to this value")
    row += 2

    # ── Per-app section ──────────────────────────────────────────────────────
    n_ovr = sum(1 for a in apps if a.has_override)
    filtered = _filter_apps(apps, ui.search_query)
    n_match = len(filtered)

    _put(scr, row, 2, "Per-App Overrides", curses.A_BOLD)
    if ui.search_query:
        _put(scr, row, 20,
             f" ({n_match} of {len(apps)} match · {n_ovr} active · d=toggle)",
             C_DIM)
    else:
        _put(scr, row, 20,
             f" ({n_ovr} active · {len(apps)} apps · d=toggle · a=apply-all)",
             C_DIM)
    row += 1
    _put(scr, row, 2, "─" * min(w - 4, 56), C_DIM)
    row += 1

    max_vis = _max_app_rows(h)

    # Keep cursor in view relative to filtered list
    app_idx = ui.cursor - GLOBAL_ROWS
    if app_idx >= 0:
        if app_idx < ui.scroll:
            ui.scroll = app_idx
        elif app_idx >= ui.scroll + max_vis:
            ui.scroll = app_idx - max_vis + 1
    if ui.scroll > max(0, n_match - max_vis):
        ui.scroll = max(0, n_match - max_vis)

    page = filtered[ui.scroll: ui.scroll + max_vis]

    for i, app in enumerate(page):
        abs_idx = ui.scroll + i
        sel = (ui.cursor == GLOBAL_ROWS + abs_idx)
        dirty = "*" if app.dirty else " "

        # name with search highlight
        prefix = f"  {dirty}"
        _put(scr, row, 2, prefix, C_SEL if sel else 0)
        name_w = 18
        name_disp = app.name[:name_w].ljust(name_w)
        hi_attr = (curses.color_pair(3) | curses.A_BOLD |
                   (curses.A_REVERSE if sel else 0))
        _put_name_highlighted(scr, row, 2 + len(prefix), name_disp,
                              ui.search_query,
                              C_SEL if sel else 0, hi_attr)
        col = 2 + len(prefix) + name_w + 1

        if app.has_override:
            _put(scr, row, col, "[ - ]", C_CTRL if sel else C_DIM)
            val_s = f"  {fmt(app.scale)}  "
            _put(scr, row, col + 5, val_s, curses.A_BOLD if sel else 0)
            _put(scr, row, col + 5 + len(val_s), "[ + ]",
                 C_CTRL if sel else C_DIM)
            seen: set = set()
            hints = []
            for occ in app.occurrences:
                tag = occ.path.stem[:12]
                if tag not in seen:
                    hints.append(tag)
                    seen.add(tag)
            _put(scr, row, col + 5 + len(val_s) + 6,
                 "  " + " · ".join(hints), C_DIM)
        else:
            _put(scr, row, col,
                 "(no override — inherits global)",
                 C_DIM if not sel else C_SEL)
        row += 1

    # scroll indicator
    if ui.scroll > 0 or ui.scroll + max_vis < n_match:
        ind = f"  {ui.scroll + 1}–{min(ui.scroll + max_vis, n_match)}/{n_match} ↑↓  "
        _put(scr, row, w - len(ind) - 1, ind, C_DIM)

    # ── help bar / search bar ────────────────────────────────────────────────
    if ui.search_mode or ui.search_query:
        # Search bar replaces help bar
        prompt = "/"
        query_disp = ui.search_query
        cursor_char = "█" if ui.search_mode else " "
        match_info = (f"  {n_match}/{len(apps)} match{'es' if n_match != 1 else ''}"
                      f"  [Esc] clear  [Enter] done")
        bar = f"  {prompt}{query_disp}{cursor_char}{match_info}"
        _put(scr, h - 2, 0, " " * (w - 1), C_CTRL)
        _put(scr, h - 2, 0, bar[:w - 1], C_CTRL)
    else:
        _put(scr, h - 2, 2,
             "  [/] Search  [s] Save  [r] Reload  [d] Toggle  [a] Apply-all  [q] Quit",
             C_DIM)

    # ── status bar ──────────────────────────────────────────────────────────
    s_attr = {"ok": C_OK, "error": C_ERR, "warn": C_WARN}.get(ui.status_kind, 0)
    any_dirty = gs.dirty or any(a.dirty for a in apps)
    _put(scr, h - 1, 0, " " * (w - 1))
    _put(scr, h - 1, 1, ui.status_msg[:w - 12], s_attr)
    if any_dirty:
        tag = "[unsaved]"
        _put(scr, h - 1, w - len(tag) - 1, tag, C_WARN | curses.A_BOLD)

    scr.refresh()


# ── input ────────────────────────────────────────────────────────────────────

def handle_key(key: int, gs: GlobalSettings, apps: list, ui: UIState) -> bool:
    visible = _filter_apps(apps, ui.search_query)
    total = GLOBAL_ROWS + len(visible)
    non_quit = True

    # ── search mode: intercept printable chars and backspace ─────────────────
    if ui.search_mode:
        if key in (curses.KEY_BACKSPACE, 127, 8):
            ui.search_query = ui.search_query[:-1]
            ui.scroll = 0
            ui.cursor = GLOBAL_ROWS
            return False
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            ui.search_mode = False
            return False
        elif key == 27:   # Escape — clear and exit search
            ui.search_mode = False
            ui.search_query = ""
            ui.scroll = 0
            ui.cursor = GLOBAL_ROWS
            return False
        elif 32 <= key <= 126 and chr(key) not in ('/'):
            ui.search_query += chr(key)
            ui.scroll = 0
            ui.cursor = GLOBAL_ROWS
            return False
        # fall through for arrow keys / action keys even in search mode

    # ── navigation ───────────────────────────────────────────────────────────
    if key == curses.KEY_UP:
        ui.cursor = (ui.cursor - 1) % total
    elif key in (curses.KEY_DOWN, ord('\t')):
        ui.cursor = (ui.cursor + 1) % total

    # ── scale adjustment ─────────────────────────────────────────────────────
    elif key in (curses.KEY_LEFT, ord('-'), ord('_')):
        _adjust(gs, visible, ui, -1)
    elif key in (curses.KEY_RIGHT, ord('+'), ord('=')):
        _adjust(gs, visible, ui, +1)

    # ── actions ──────────────────────────────────────────────────────────────
    elif key == ord('/'):
        ui.search_mode = True
        ui.search_query = ""
        ui.scroll = 0
        ui.cursor = GLOBAL_ROWS

    elif key in (ord('a'), ord('A')):
        _apply_all(apps, ui)
    elif key in (ord('s'), ord('S')):
        save_all(gs, apps, ui)
    elif key in (ord('r'), ord('R')):
        _reload(gs, apps, ui)
        ui.search_query = ""
        ui.search_mode = False
    elif key in (ord('d'), ord('D')):
        _toggle_override(visible, ui)

    elif key in (ord('q'), ord('Q')):
        if ui.search_mode or ui.search_query:
            # q exits search first
            ui.search_mode = False
            ui.search_query = ""
            ui.scroll = 0
            ui.cursor = GLOBAL_ROWS
        else:
            non_quit = False
            any_dirty = gs.dirty or any(a.dirty for a in apps)
            if any_dirty and not ui.quit_armed:
                ui.status_msg = "Unsaved changes! Press [q] again to quit, [s] to save."
                ui.status_kind = "warn"
                ui.quit_armed = True
                return False
            return True

    elif key == 27:  # Escape outside search mode
        if ui.search_query:
            ui.search_mode = False
            ui.search_query = ""
            ui.scroll = 0
            ui.cursor = GLOBAL_ROWS
        else:
            non_quit = False
            any_dirty = gs.dirty or any(a.dirty for a in apps)
            if any_dirty and not ui.quit_armed:
                ui.status_msg = "Unsaved changes! Press [q] again to quit, [s] to save."
                ui.status_kind = "warn"
                ui.quit_armed = True
                return False
            return True

    if non_quit:
        ui.quit_armed = False
    return False


def _adjust(gs: GlobalSettings, visible: list, ui: UIState, direction: int):
    c = ui.cursor
    if c == 0:
        new = max(1, min(8, int(gs.gdk_scale) + direction))
        gs.gdk_scale = float(new)
        ui.status_msg = f"GDK_SCALE → {new}"
        ui.status_kind = "info"
    elif c == 1:
        if gs.monitor_scale == 'auto':
            if direction > 0:
                gs.monitor_scale = fmt_mon(MIN_MON)
        else:
            cur = float(gs.monitor_scale)
            nv = cur + direction * MON_STEP
            gs.monitor_scale = 'auto' if nv < MIN_MON else fmt_mon(min(MAX_MON, nv))
        ui.status_msg = f"Monitor scale → {gs.monitor_scale}"
        ui.status_kind = "info"
    elif c == 2:
        nv = round((ui.apply_all + direction * SCALE_STEP) / SCALE_STEP) * SCALE_STEP
        ui.apply_all = max(MIN_APP, min(MAX_APP, round(nv, 10)))
        ui.status_msg = f"Apply-all value → {fmt(ui.apply_all)}  (press [a] to apply)"
        ui.status_kind = "info"
    else:
        idx = c - GLOBAL_ROWS
        if idx < len(visible):
            app = visible[idx]
            if app.has_override:
                nv = round((app.scale + direction * SCALE_STEP) / SCALE_STEP) * SCALE_STEP
                app.scale = max(MIN_APP, min(MAX_APP, round(nv, 10)))
                ui.status_msg = f"{app.name} → {fmt(app.scale)}"
                ui.status_kind = "info"
            else:
                app.scale = ui.apply_all
                app.has_override = True
                ui.status_msg = (f"{app.name}: override activated at {fmt(app.scale)} "
                                 f"— adjust with ←→, save with [s]")
                ui.status_kind = "info"


def _apply_all(apps: list, ui: UIState):
    for app in apps:
        app.scale = ui.apply_all
        app.has_override = True
    n = len(apps)
    ui.status_msg = f"Apply-all: {n} apps set to {fmt(ui.apply_all)} — save with [s]"
    ui.status_kind = "info"


def _reload(gs: GlobalSettings, apps: list, ui: UIState):
    new_gs = load_global()
    gs.__dict__.update(new_gs.__dict__)
    new_apps = discover_apps()
    apps.clear()
    apps.extend(new_apps)
    ui.scroll = 0
    ui.cursor = min(ui.cursor, GLOBAL_ROWS + max(0, len(apps) - 1))
    n_ovr = sum(1 for a in apps if a.has_override)
    ui.status_msg = (f"Reloaded. GDK_SCALE={int(gs.gdk_scale)}  "
                     f"{n_ovr} overrides  {len(apps)} apps")
    ui.status_kind = "info"


def _toggle_override(visible: list, ui: UIState):
    idx = ui.cursor - GLOBAL_ROWS
    if idx < 0 or idx >= len(visible):
        ui.status_msg = "Select an app row to toggle its override."
        ui.status_kind = "warn"
        return
    app = visible[idx]
    if app.has_override:
        app.has_override = False
        ui.status_msg = f"{app.name}: override removed — save with [s]"
    else:
        app.scale = ui.apply_all
        app.has_override = True
        ui.status_msg = f"{app.name}: override activated at {fmt(app.scale)} — save with [s]"
    ui.status_kind = "info"


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

    gs   = load_global()
    apps = discover_apps()
    ui   = UIState()

    # Seed apply_all from existing overrides if they agree
    active_scales = [a.scale for a in apps if a.has_override]
    if active_scales and len({round(s, 2) for s in active_scales}) == 1:
        ui.apply_all = active_scales[0]

    n_ovr = sum(1 for a in apps if a.has_override)
    ui.status_msg  = (f"Loaded. GDK_SCALE={int(gs.gdk_scale)}  "
                      f"{n_ovr} overrides  {len(apps)} apps")
    ui.status_kind = "info"

    while True:
        draw(scr, gs, apps, ui)
        key = scr.getch()
        if key == curses.KEY_RESIZE:
            continue
        if handle_key(key, gs, apps, ui):
            break


if __name__ == '__main__':
    curses.wrapper(main)
