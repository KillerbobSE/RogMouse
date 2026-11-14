"""ASUS ROG mouse HID protocol (no Qt — usable from CLI/tests).

Talks to the ASUS ROG Keris II Origin through the ROG Omni Receiver
(0b05:1ace). The receiver's vendor interface uses 64-byte reports with report
id 3; the payload after the id is the classic ASUS packet:

    request : CMD_LO CMD_HI params...
    response: CMD_LO CMD_HI results...      (ff aa = mouse asleep/off)

Byte offsets below ("raw") are into the packet *without* the report id, the
same convention libratbag's asus driver uses. Every command here was verified
on a Keris II Origin (write → read back → restore):

    0x0712 battery       raw[4]=%  raw[7:9]=mV (LE)  raw[9]=charging flag
    0x0012 profile info  raw[10]=profile index  raw[11]=active DPI preset (1-based)
    0x0412 p0=2  DPI     raw[4+4i:6+4i]=x, raw[6+4i:8+4i]=y   (value*50+50)
    0x0412 p0=0  setting raw[14]=debounce index  raw[16]=angle snapping
    0x0312 p0=led LED    raw[4]=mode raw[5]=brightness 0-100 raw[6:9]=RGB
                         (led 0-2; zones 1 and 2 are the wheel and side, 3+ doesn't answer)
    0x0512 p0=0  buttons raw[4+2i]=action code, raw[5+2i]=action type
    0x3151 set setting   p0=field (0-3 DPI preset, 5 debounce, 6 snapping), p2..=value
    0x2851 set LED       p0=led, p2=mode, p3=brightness, p4-6=RGB
    0x2151 set button    p2=physical button code, p3=1, p4=action code, p5=type
    0x0250 set profile   p0=index
    0x0350 save to onboard memory

Wired mode (mouse on its USB cable, 0b05:1c0c) is tried with the same packets
but is untested.
"""

from __future__ import annotations

import glob
import os
import select
import threading
import time
from dataclasses import dataclass, field

VENDOR = 0x0B05
RECEIVER_PIDS = {0x1ACE}          # ROG Omni Receiver
WIRED_PIDS = {0x1C0C}             # Keris II Origin over USB cable

CMD_GET_PROFILE = 0x0012
CMD_GET_LED = 0x0312
CMD_GET_SETTINGS = 0x0412
CMD_GET_BUTTONS = 0x0512
CMD_GET_BATTERY = 0x0712
CMD_SET_PROFILE = 0x0250
CMD_SAVE = 0x0350
CMD_SET_BUTTON = 0x2151
CMD_SET_LED = 0x2851
CMD_SET_SETTING = 0x3151

STATUS_ASLEEP = b"\xff\xaa"

NUM_PROFILES = 5
NUM_DPI = 4
DPI_MIN, DPI_MAX, DPI_STEP = 100, 42000, 50
DEBOUNCE_MS = (4, 8, 12, 16, 20, 24, 28, 32)
FIELD_DEBOUNCE = NUM_DPI + 1
FIELD_SNAPPING = NUM_DPI + 2

LED_MODES = {           # index → name (ASUS order)
    0: "Static",
    1: "Breathing",
    2: "Colour cycle",
    3: "Rainbow wave",
    4: "Reactive",
    6: "Battery level",
}
LED_MODES_WITH_COLOR = {0, 1, 4}
LED_STATIC = 0
NUM_LEDS = 3                # lighting zones, all set together


def battery_rgb(pct: int) -> tuple[int, int, int]:
    """Smooth green → yellow → red for a battery percentage.

    Hue runs from 120° (green) at >=90 % through 60° (yellow) at 50 % to 0°
    (red) at <=10 %, at full saturation/value so the LEDs stay vivid.
    """
    t = min(1.0, max(0.0, (pct - 10) / 80))
    if t >= 0.5:                     # yellow → green: drop red
        return round(255 * (1 - t) * 2), 255, 0
    return 255, round(255 * t * 2), 0  # red → yellow: add green

# Physical buttons in slot order, identified by their default action code.
BUTTONS = [
    (0xF0, "Left button"),
    (0xF1, "Right button"),
    (0xF2, "Middle button"),
    (0xE4, "Back (side)"),
    (0xE5, "Forward (side)"),
    (0xE6, "DPI button"),
    (0xE8, "Wheel up"),
    (0xE9, "Wheel down"),
]
TYPE_BUTTON = 1
# Actions a button can be mapped to (code, label).
ACTIONS = [
    (0xF0, "Left click"),
    (0xF1, "Right click"),
    (0xF2, "Middle click"),
    (0xE4, "Back"),
    (0xE5, "Forward"),
    (0xE6, "DPI cycle"),
    (0xE8, "Scroll up"),
    (0xE9, "Scroll down"),
]
ACTION_NAMES = dict(ACTIONS)

# One HID transaction at a time across all threads.
_io_lock = threading.Lock()


class NoDevice(Exception):
    pass


class NoPermission(Exception):
    pass


class Asleep(Exception):
    """The receiver is there but the mouse doesn't answer (asleep/off)."""


@dataclass
class Reading:
    percent: int
    millivolts: int
    charging: bool
    wired: bool          # read over the USB cable


@dataclass
class Led:
    mode: int
    brightness: int      # 0-100
    rgb: tuple[int, int, int]


@dataclass
class State:
    profile: int                         # 0-based
    dpi: list[int]                       # NUM_DPI presets
    active_dpi: int                      # 0-based index, -1 unknown
    debounce_ms: int
    snapping: bool
    led: Led                             # zone 0 (shown in the menu)
    leds: list[Led] = field(default_factory=list)      # every zone
    buttons: list[int] = field(default_factory=list)   # action code per slot

    @property
    def current_dpi(self) -> int | None:
        return self.dpi[self.active_dpi] if 0 <= self.active_dpi < len(self.dpi) else None


@dataclass
class Node:
    path: str
    pid: int
    report_id: int | None   # None = device uses unnumbered reports


def _parse_hid_id(uevent: str) -> tuple[int, int] | None:
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            _bus, vid, pid = line.split("=", 1)[1].split(":")
            return int(vid, 16), int(pid, 16)
    return None


def find_nodes() -> list[Node]:
    """Vendor-page hidraw nodes of supported ROG devices, wired first."""
    nodes: list[Node] = []
    for h in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(f"{h}/device/uevent") as f:
                ids = _parse_hid_id(f.read())
            with open(f"{h}/device/report_descriptor", "rb") as f:
                desc = f.read()
        except OSError:
            continue
        if not ids or ids[0] != VENDOR or ids[1] not in RECEIVER_PIDS | WIRED_PIDS:
            continue
        # Vendor-defined usage page: 06 xx ff
        if len(desc) < 3 or desc[0] != 0x06 or desc[2] != 0xFF:
            continue
        if b"\x85\x03" in desc:
            rid = 3
        elif b"\x85" in desc[:12]:
            rid = desc[desc.index(b"\x85") + 1]
        else:
            rid = None
        nodes.append(Node("/dev/" + os.path.basename(h), ids[1], rid))
    nodes.sort(key=lambda n: n.pid not in WIRED_PIDS)
    return nodes


class Mouse:
    """One open connection to the mouse. Use as a context manager."""

    def __init__(self, node: Node):
        self.node = node
        self.wired = node.pid in WIRED_PIDS
        self.fd = os.open(node.path, os.O_RDWR | os.O_NONBLOCK)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.fd)

    # ---- transport ----
    def query(self, cmd: int, *params: int, tries: int = 6, timeout: float = 0.5) -> bytes:
        """Send a command and return the response packet (report id stripped)."""
        rid = self.node.report_id
        prefix = bytes([rid]) if rid is not None else b""
        req = (prefix + bytes([cmd & 0xFF, cmd >> 8, *params])).ljust(64, b"\0")
        off = len(prefix)
        with _io_lock:
            for attempt in range(tries):
                while select.select([self.fd], [], [], 0.02)[0]:
                    os.read(self.fd, 64)
                os.write(self.fd, req)
                deadline = time.monotonic() + timeout
                while (left := deadline - time.monotonic()) > 0 and \
                        select.select([self.fd], [], [], left)[0]:
                    r = os.read(self.fd, 64)[off:]
                    if r[:2] == req[off:off + 2]:
                        return r
                    if r[:2] == STATUS_ASLEEP:
                        break
                if attempt + 1 < tries:
                    time.sleep(0.3)
        raise Asleep("Mouse not responding — move it to wake it")

    # ---- reads ----
    def battery(self) -> Reading:
        r = self.query(CMD_GET_BATTERY)
        pct = r[4]
        if not 0 < pct <= 100:
            raise Asleep("Mouse not responding — move it to wake it")
        return Reading(pct, int.from_bytes(r[7:9], "little"), bool(r[9]), self.wired)

    def state(self) -> State:
        prof = self.query(CMD_GET_PROFILE)
        xy = self.query(CMD_GET_SETTINGS, 2)
        s0 = self.query(CMD_GET_SETTINGS, 0)
        leds = []
        for i in range(NUM_LEDS):
            r = self.query(CMD_GET_LED, i)
            leds.append(Led(r[4], r[5], (r[6], r[7], r[8])))
        btn = self.query(CMD_GET_BUTTONS, 0)
        dpi = [int.from_bytes(xy[4 + 4 * i:6 + 4 * i], "little") * 50 + 50 for i in range(NUM_DPI)]
        deb = s0[14]
        return State(
            profile=prof[10],
            dpi=dpi,
            active_dpi=prof[11] - 1 if prof[11] else -1,
            debounce_ms=DEBOUNCE_MS[deb] if deb < len(DEBOUNCE_MS) else -1,
            snapping=bool(s0[16]),
            led=leds[0],
            leds=leds,
            buttons=[btn[4 + 2 * i] for i in range(len(BUTTONS))],
        )

    # ---- writes (each followed by save() so it sticks in onboard memory) ----
    def save(self) -> None:
        self.query(CMD_SAVE)

    def set_profile(self, index: int) -> None:
        if not 0 <= index < NUM_PROFILES:
            raise ValueError("bad profile")
        self.query(CMD_SET_PROFILE, index)

    def set_dpi(self, preset: int, dpi: int) -> None:
        if not 0 <= preset < NUM_DPI or not DPI_MIN <= dpi <= DPI_MAX:
            raise ValueError("bad DPI")
        v = (dpi - 50) // 50
        self.query(CMD_SET_SETTING, preset, 0, v & 0xFF, v >> 8)
        self.save()

    def set_debounce(self, ms: int) -> None:
        self.query(CMD_SET_SETTING, FIELD_DEBOUNCE, 0, DEBOUNCE_MS.index(ms))
        self.save()

    def set_snapping(self, on: bool) -> None:
        self.query(CMD_SET_SETTING, FIELD_SNAPPING, 0, int(on))
        self.save()

    def set_led(self, mode: int, brightness: int, rgb: tuple[int, int, int]) -> None:
        if mode not in LED_MODES or not 0 <= brightness <= 100:
            raise ValueError("bad LED setting")
        for i in range(NUM_LEDS):
            self.query(CMD_SET_LED, i, 0, mode, brightness, *rgb)
        self.save()

    def set_button(self, slot: int, action: int) -> None:
        src = BUTTONS[slot][0]
        if src == 0xF0 and action != 0xF0:
            raise ValueError("Left button stays left click (so you can't lock yourself out)")
        if action not in ACTION_NAMES:
            raise ValueError("bad action")
        self.query(CMD_SET_BUTTON, 0, 0, src, TYPE_BUTTON, action, TYPE_BUTTON)
        self.save()

    def reset_buttons(self) -> None:
        for src, _ in BUTTONS:
            self.query(CMD_SET_BUTTON, 0, 0, src, TYPE_BUTTON, src, TYPE_BUTTON)
        self.save()


def open_mouse() -> Mouse:
    """Open the first node that answers. Raises NoDevice / NoPermission / Asleep."""
    nodes = find_nodes()
    if not nodes:
        raise NoDevice("No ROG Omni Receiver or wired ROG mouse found")
    denied = False
    for node in nodes:
        try:
            return Mouse(node)
        except PermissionError:
            denied = True
    if denied:
        raise NoPermission("No access to the mouse — install the udev rule (see README)")
    raise NoDevice("Could not open the mouse")


def read_battery() -> Reading:
    with open_mouse() as m:
        return m.battery()
