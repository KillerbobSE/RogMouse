"""RogMouse tray — battery indicator and settings for the ASUS ROG Keris II Origin.

Shows the charge as a battery-shaped tray icon, notifies when it gets low or
finishes charging, and exposes the mouse's onboard settings (profile, DPI,
lighting, buttons, button response, angle snapping) from the tray menu.
All HID traffic happens on a worker thread so the panel never blocks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QColorDialog, QInputDialog, QMenu, QSystemTrayIcon

from . import protocol as P

APP_NAME = "RogMouse"
CONFIG = Path.home() / ".config" / "rogmouse" / "config.json"
DEFAULTS = {"interval_min": 5, "low_alert": 20}
INTERVALS = (1, 5, 15, 30)
ALERTS = (0, 10, 20, 30)
RETRY_ASLEEP_S = 60          # asleep: try again sooner than the normal interval
STATE_MAX_AGE_S = 20         # re-read settings when the menu opens after this
DPI_CHOICES = (400, 800, 1000, 1200, 1600, 2000, 2400, 3200, 4000, 6400, 8000, 12000, 16000, 26000, 42000)
BRIGHTNESS = ((0, "Off"), (25, "25%"), (50, "50%"), (75, "75%"), (100, "100%"))


def load_config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def level_color(pct: int | None) -> QColor:
    if pct is None:
        return QColor("#8a8a8a")
    if pct <= 15:
        return QColor("#e5484d")
    if pct <= 30:
        return QColor("#f5a524")
    return QColor("#46a758")


def battery_icon(pct: int | None, charging: bool = False, stale: bool = False) -> QIcon:
    """Draw a battery glyph with the fill level and the number inside."""
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        s = size / 64.0
        body = QRectF(2 * s, 14 * s, 54 * s, 36 * s)
        nub = QRectF(56 * s, 25 * s, 6 * s, 14 * s)
        outline = QColor("#d0d0d0") if not stale else QColor("#7a7a7a")
        p.setPen(QPen(outline, max(1.0, 4 * s)))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(body, 6 * s, 6 * s)
        p.setPen(Qt.NoPen)
        p.setBrush(outline)
        p.drawRoundedRect(nub, 2 * s, 2 * s)
        if pct is not None:
            inner = body.adjusted(5 * s, 5 * s, -5 * s, -5 * s)
            inner.setWidth(max(2 * s, inner.width() * pct / 100))
            p.setBrush(level_color(None if stale else pct))
            p.drawRoundedRect(inner, 2 * s, 2 * s)
        text = "?" if pct is None else str(pct)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int((26 if len(text) < 3 else 21) * s))
        p.setFont(f)
        for dx, dy, c in ((1, 1, QColor(0, 0, 0, 160)), (0, 0, QColor("white"))):
            p.setPen(c)
            p.drawText(body.translated(dx * s, dy * s), Qt.AlignCenter, text)
        if charging and not stale:
            # small lightning badge in the top-right corner
            bolt = QRectF(36 * s, 0, 28 * s, 28 * s)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#f5d90a"))
            p.drawEllipse(bolt)
            pts = [(53, 3), (42, 15), (49, 15), (45, 25), (57, 11), (50, 11), (53, 3)]
            p.setBrush(QColor("#1a1a1a"))
            p.drawPolygon(QPolygonF([QPointF(x * s, y * s) for x, y in pts]))
        p.end()
        icon.addPixmap(pm)
    return icon


def swatch(rgb: tuple[int, int, int]) -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(QColor(*rgb))
    return QIcon(pm)


class Worker(QObject):
    """Runs mouse I/O off the GUI thread; results come back as signals."""

    battery = Signal(object, str)    # (Reading | None, error)
    state = Signal(object, str)      # (State | None, error)
    failed = Signal(str)             # a write failed

    def __init__(self):
        super().__init__()
        self._battery_busy = threading.Lock()
        self._state_busy = threading.Lock()

    @staticmethod
    def _errmsg(e: Exception) -> str:
        return str(e) if isinstance(e, (P.NoDevice, P.NoPermission, P.Asleep, ValueError)) \
            else f"Error: {e}"

    def poll_battery(self) -> None:
        if not self._battery_busy.acquire(blocking=False):
            return

        def work():
            try:
                self.battery.emit(P.read_battery(), "")
            except Exception as e:
                self.battery.emit(None, self._errmsg(e))
            finally:
                self._battery_busy.release()
        threading.Thread(target=work, daemon=True).start()

    def read_state(self) -> None:
        if not self._state_busy.acquire(blocking=False):
            return

        def work():
            try:
                with P.open_mouse() as m:
                    self.state.emit(m.state(), "")
            except Exception as e:
                self.state.emit(None, self._errmsg(e))
            finally:
                self._state_busy.release()
        threading.Thread(target=work, daemon=True).start()

    def write(self, fn, *args) -> None:
        """Run fn(mouse, *args) then re-read the state."""
        def work():
            try:
                with P.open_mouse() as m:
                    fn(m, *args)
                    st = m.state()
                self.state.emit(st, "")
            except Exception as e:
                self.failed.emit(self._errmsg(e))
                self.read_state()
        threading.Thread(target=work, daemon=True).start()


class Tray(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.cfg = load_config()
        self.last = None            # last good P.Reading
        self.last_time = 0.0
        self.alerted_at = None      # low-battery stage already notified
        self.full_notified = False
        self.state: P.State | None = None
        self.state_time = 0.0
        self.state_error = "Reading settings…"

        self.icon = QSystemTrayIcon(battery_icon(None), app)
        self.worker = Worker()
        self.worker.battery.connect(self.on_battery)
        self.worker.state.connect(self.on_state)
        self.worker.failed.connect(self.on_write_failed)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.worker.poll_battery)

        self.build_menu()
        self.icon.activated.connect(
            lambda r: self.refresh() if r == QSystemTrayIcon.Trigger else None
        )
        self.icon.show()
        self.refresh()
        self.worker.read_state()

    # ------------------------------------------------------------------ menu
    def build_menu(self) -> None:
        m = QMenu()
        m.aboutToShow.connect(self.on_menu_open)
        self.status_action = m.addAction("Reading…")
        self.status_action.setEnabled(False)
        self.detail_action = m.addAction("")
        self.detail_action.setEnabled(False)
        m.addSeparator()

        self.settings_note = m.addAction("")
        self.settings_note.setEnabled(False)
        self.profile_menu = m.addMenu("Profile")
        self.dpi_menu = m.addMenu("DPI")
        self.light_menu = m.addMenu("Lighting")
        self.button_menu = m.addMenu("Buttons")
        self.perf_menu = m.addMenu("Performance")
        self.settings_menus = (self.profile_menu, self.dpi_menu, self.light_menu,
                               self.button_menu, self.perf_menu)
        self.build_settings()
        m.addSeparator()

        m.addAction("Refresh now", self.refresh)
        app_menu = m.addMenu("Tray settings")
        self._header(app_menu, "Check battery every")
        self._check(self._radio(app_menu, [(v, f"{v} min") for v in INTERVALS],
                                lambda v: self.set_cfg("interval_min", v)), self.cfg["interval_min"])
        app_menu.addSeparator()
        self._header(app_menu, "Low battery alert")
        self._check(self._radio(app_menu, [(v, "Off" if v == 0 else f"Below {v}%") for v in ALERTS],
                                lambda v: self.set_cfg("low_alert", v)), self.cfg["low_alert"])
        m.addSeparator()
        m.addAction("Quit", self.app.quit)
        self.menu = m
        self.icon.setContextMenu(m)
        self.render_settings()

    def on_menu_open(self) -> None:
        if time.time() - self.state_time > STATE_MAX_AGE_S:
            self.worker.read_state()
        if self.last:
            self.render()   # refresh the "updated N min ago" text

    # ------------------------------------------------------------ menu helpers
    # The tray menu is exported over D-Bus (dbusmenu) and every QAction gets an
    # id there. COSMIC keeps showing the layout it fetched, so actions must be
    # created ONCE and only updated in place — recreating them changes the ids
    # and clicks on the old ids are silently dropped. COSMIC also only expands
    # one level of submenus, so every submenu is a flat list.

    @staticmethod
    def _header(menu: QMenu, text: str) -> QAction:
        a = menu.addAction(text)
        a.setEnabled(False)
        return a

    @staticmethod
    def _radio(menu: QMenu, items, on_pick) -> dict:
        group = QActionGroup(menu)
        group.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        acts = {}
        for value, label in items:
            a = QAction(label, menu, checkable=True)
            a.triggered.connect(lambda _=False, v=value: on_pick(v))
            group.addAction(a)
            menu.addAction(a)
            acts[value] = a
        return acts

    @staticmethod
    def _check(acts: dict, current) -> None:
        for v, a in acts.items():
            a.setChecked(v == current)

    def build_settings(self) -> None:
        # Profile
        self.profile_acts = self._radio(
            self.profile_menu, [(i, f"Profile {i + 1}") for i in range(P.NUM_PROFILES)],
            lambda v: self.write(P.Mouse.set_profile, v))

        # DPI
        dm = self.dpi_menu
        self.dpi_header = self._header(dm, "Current DPI")
        self.dpi_acts = self._radio(dm, [(d, str(d)) for d in DPI_CHOICES] + [(-1, "")],
                                    self.pick_current_dpi)
        dm.addSeparator()
        self._header(dm, "DPI button cycles through")
        self.preset_acts = [dm.addAction("", lambda i=i: self.edit_dpi(i))
                            for i in range(P.NUM_DPI)]

        # Lighting
        lm = self.light_menu
        self._header(lm, "Effect")
        self.led_mode_acts = self._radio(lm, list(P.LED_MODES.items()), self.pick_led_mode)
        lm.addSeparator()
        self._header(lm, "Brightness")
        self.bright_acts = self._radio(lm, BRIGHTNESS, self.pick_brightness)
        lm.addSeparator()
        self.colour_act = lm.addAction("Colour…", self.pick_colour)

        # Buttons
        bm = self.button_menu
        self.button_acts = [bm.addAction("", lambda slot=slot: self.edit_button(slot))
                            for slot in range(len(P.BUTTONS))]
        bm.addSeparator()
        self.reset_act = bm.addAction("Restore default buttons",
                                      lambda: self.write(P.Mouse.reset_buttons))

        # Performance
        pm = self.perf_menu
        self._header(pm, "Button response")
        self.deb_acts = self._radio(
            pm, [(ms, f"{ms} ms" + ("  (fastest)" if ms == 4 else "")) for ms in P.DEBOUNCE_MS],
            lambda v: self.write(P.Mouse.set_debounce, v))
        pm.addSeparator()
        self.snap_act = QAction("Angle snapping", pm, checkable=True)
        self.snap_act.triggered.connect(lambda on: self.write(P.Mouse.set_snapping, on))
        pm.addAction(self.snap_act)

    def write(self, fn, *args) -> None:
        if self.state is not None:
            self.worker.write(fn, *args)

    # ------------------------------------------------------------ menu render
    def render_settings(self) -> None:
        """Update the existing actions in place from self.state."""
        st = self.state
        for sub in self.settings_menus:
            sub.setEnabled(st is not None)
        self.settings_note.setVisible(st is None)
        self.settings_note.setText(self.state_error or "Mouse settings unavailable")
        if st is None:
            return

        self.profile_menu.setTitle(f"Profile: {st.profile + 1}")
        self._check(self.profile_acts, st.profile)

        cur = st.current_dpi
        self.dpi_menu.setTitle(f"DPI: {cur}" if cur else "DPI")
        self.dpi_header.setText(f"Current DPI (preset {st.active_dpi + 1})" if cur else "Current DPI")
        extra = self.dpi_acts[-1]
        extra.setVisible(bool(cur) and cur not in DPI_CHOICES)
        extra.setText(str(cur))
        self._check(self.dpi_acts, cur if cur in DPI_CHOICES else -1)
        for i, a in enumerate(self.preset_acts):
            active = "  ◀ active" if i == st.active_dpi else ""
            a.setText(f"Preset {i + 1}: {st.dpi[i]}{active}  …")

        led = st.led
        self.light_menu.setTitle(f"Lighting: {P.LED_MODES.get(led.mode, '?')}"
                                 + (" (off)" if led.brightness == 0 else ""))
        self._check(self.led_mode_acts, led.mode)
        self._check(self.bright_acts, led.brightness)
        has_colour = led.mode in P.LED_MODES_WITH_COLOR
        self.colour_act.setEnabled(has_colour)
        self.colour_act.setIcon(swatch(led.rgb) if has_colour else QIcon())
        self.colour_act.setText("Colour…" if has_colour
                                else "Colour: Static, Breathing or Reactive only")

        changed_any = False
        for slot, ((src, name), a) in enumerate(zip(P.BUTTONS, self.button_acts)):
            code = st.buttons[slot] if slot < len(st.buttons) else src
            label = P.ACTION_NAMES.get(code, f"0x{code:02x}")
            if src == 0xF0:
                a.setText(f"{name}: {label}  (locked)")
                a.setEnabled(False)
                continue
            changed_any |= code != src
            a.setText(f"{name}: {label}{'  •' if code != src else ''}  …")
        self.reset_act.setEnabled(changed_any)

        self._check(self.deb_acts, st.debounce_ms)
        self.snap_act.setChecked(st.snapping)

    # ------------------------------------------------------------ menu actions
    def pick_current_dpi(self, dpi: int) -> None:
        st = self.state
        if st and dpi > 0 and 0 <= st.active_dpi < P.NUM_DPI:
            self.write(P.Mouse.set_dpi, st.active_dpi, dpi)

    def pick_led_mode(self, mode: int) -> None:
        if self.state:
            led = self.state.led
            rgb = led.rgb
            if mode in P.LED_MODES_WITH_COLOR and rgb == (0, 0, 0):
                rgb = (255, 0, 0)    # colour-cycle modes store black; don't go dark
            self.write(P.Mouse.set_led, mode, led.brightness or 50, rgb)

    def pick_brightness(self, value: int) -> None:
        if self.state:
            led = self.state.led
            self.write(P.Mouse.set_led, led.mode, value, led.rgb)

    def edit_dpi(self, preset: int) -> None:
        if not self.state:
            return
        current = self.state.dpi[preset]
        items = [str(d) for d in sorted(set(DPI_CHOICES) | {current})]
        text, ok = QInputDialog.getItem(
            None, "RogMouse", f"DPI for preset {preset + 1} ({P.DPI_MIN}–{P.DPI_MAX}):",
            items, items.index(str(current)), True)
        if not ok:
            return
        try:
            val = int(text.strip())
        except ValueError:
            notify("Invalid DPI", f"'{text}' is not a number")
            return
        val = min(P.DPI_MAX, max(P.DPI_MIN, round(val / P.DPI_STEP) * P.DPI_STEP))
        if val != current:
            self.write(P.Mouse.set_dpi, preset, val)

    def edit_button(self, slot: int) -> None:
        if not self.state:
            return
        src, name = P.BUTTONS[slot]
        code = self.state.buttons[slot]
        actions = [(c, n) for c, n in P.ACTIONS if c != 0xF0 or src == 0xF1]
        labels = [n + ("  (default)" if c == src else "") for c, n in actions]
        codes = [c for c, _ in actions]
        cur = codes.index(code) if code in codes else 0
        text, ok = QInputDialog.getItem(None, "RogMouse", f"{name} does:", labels, cur, False)
        if ok:
            new = codes[labels.index(text)]
            if new != code:
                self.write(P.Mouse.set_button, slot, new)

    def pick_colour(self) -> None:
        if not self.state:
            return
        led = self.state.led
        c = QColorDialog.getColor(QColor(*led.rgb), None, "Mouse lighting colour")
        if c.isValid():
            self.worker.write(P.Mouse.set_led, led.mode, led.brightness or 50,
                              (c.red(), c.green(), c.blue()))

    def set_cfg(self, key: str, value: int) -> None:
        self.cfg[key] = value
        save_config(self.cfg)
        if key == "interval_min":
            self.schedule(value * 60)
        else:
            self.alerted_at = None
            self.check_alerts()

    # --------------------------------------------------------------- results
    def on_state(self, st, error: str) -> None:
        if st is not None:
            unchanged = st == self.state
            self.state, self.state_time, self.state_error = st, time.time(), ""
            if unchanged:
                return      # don't churn the menu layout while it's open
        else:
            if self.state is None and self.state_error == error:
                return
            self.state, self.state_error = None, error
        self.render_settings()

    def on_write_failed(self, error: str) -> None:
        notify("Couldn't change mouse setting", error)

    # ----------------------------------------------------------------- battery
    def refresh(self) -> None:
        self.timer.stop()
        self.worker.poll_battery()

    def schedule(self, seconds: float) -> None:
        self.timer.start(int(seconds * 1000))

    def on_battery(self, reading, error: str) -> None:
        interval = self.cfg["interval_min"] * 60
        if reading is not None:
            self.last, self.last_time = reading, time.time()
            self.render()
            self.check_alerts()
            # poll faster while charging so "fully charged" shows up promptly
            self.schedule(min(interval, 120) if reading.charging else interval)
            if self.state is None:
                self.worker.read_state()   # mouse woke up: load settings too
            return
        self.render(problem=error or "Mouse asleep — move it to wake")
        self.schedule(min(interval, RETRY_ASLEEP_S))

    def render(self, problem: str = "") -> None:
        r = self.last
        stale = bool(problem)
        self.icon.setIcon(battery_icon(r.percent if r else None,
                                       charging=bool(r and r.charging), stale=stale))
        if r:
            ago = int((time.time() - self.last_time) / 60)
            when = "just now" if ago < 1 else f"{ago} min ago"
            head = f"Keris II Origin: {r.percent}%" + (" · charging" if r.charging else "")
            detail = f"{r.millivolts / 1000:.2f} V · updated {when}"
        else:
            head, detail = "Keris II Origin: unknown", ""
        if problem:
            detail = problem + (f" · last {detail}" if detail else "")
        self.status_action.setText(head)
        self.detail_action.setText(detail)
        self.detail_action.setVisible(bool(detail))
        tip = f"{head}\n{detail}"
        if self.state and self.state.current_dpi:
            tip += f"\nProfile {self.state.profile + 1} · {self.state.current_dpi} DPI"
        self.icon.setToolTip(tip.strip())

    def check_alerts(self) -> None:
        r = self.last
        if not r:
            return
        if r.charging:
            self.alerted_at = None
            if r.percent >= 100 and not self.full_notified:
                self.full_notified = True
                notify("Mouse fully charged", "You can unplug your ROG Keris II Origin.")
            return
        self.full_notified = False
        limit = self.cfg["low_alert"]
        if not limit:
            return
        if r.percent > limit:
            self.alerted_at = None          # recharged: re-arm
            return
        stage = "critical" if r.percent <= 5 else "low"
        if self.alerted_at == stage:
            return
        self.alerted_at = stage
        notify(f"Mouse battery {stage}: {r.percent}%",
               "Plug in your ROG Keris II Origin to charge it.",
               critical=stage == "critical")


def notify(title: str, body: str, critical: bool = False) -> None:
    try:
        subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", "input-mouse",
                          "-u", "critical" if critical else "normal", title, body])
    except OSError:
        pass


def cli() -> None:
    try:
        with P.open_mouse() as m:
            r = m.battery()
            print(f"Battery : {r.percent}%  ({r.millivolts / 1000:.2f} V)"
                  + ("  charging" if r.charging else ""))
            if "--all" in sys.argv:
                st = m.state()
                print(f"Profile : {st.profile + 1}")
                print("DPI     : " + "  ".join(
                    f"[{d}]" if i == st.active_dpi else str(d) for i, d in enumerate(st.dpi)))
                print(f"Lighting: {P.LED_MODES.get(st.led.mode, st.led.mode)}, "
                      f"{st.led.brightness}%, RGB{st.led.rgb}")
                print(f"Response: {st.debounce_ms} ms   Angle snapping: "
                      + ("on" if st.snapping else "off"))
                for (src, name), code in zip(P.BUTTONS, st.buttons):
                    print(f"  {name:15} → {P.ACTION_NAMES.get(code, hex(code))}")
    except Exception as e:
        sys.exit(str(e))


def main() -> None:
    if "--once" in sys.argv or "--all" in sys.argv:
        cli()
        return

    # Single instance: a second launch just exits.
    import fcntl
    lock = open(Path(f"/run/user/{os.getuid()}/rogmouse.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("RogMouse is already running")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("rogmouse")
    app.setQuitOnLastWindowClosed(False)

    # The tray applet may come up after us at login — wait for it a bit.
    for _ in range(30):
        if QSystemTrayIcon.isSystemTrayAvailable():
            break
        time.sleep(1)
    else:
        sys.exit("No system tray available")

    tray = Tray(app)  # noqa: F841  (keep a reference)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
