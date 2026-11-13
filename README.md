# RogMouse

Linux tray app for the ASUS ROG Keris II Origin (via the ROG Omni Receiver) — a small
stand-in for Armoury Crate.

![Tray icons: normal, low, critical, charging, asleep](docs/icons.png)

**Tray icon** — battery %, colour-coded, with a ⚡ badge while charging. Left-click refreshes.

**Right-click menu**
- Battery %, voltage, last update
- Profile 1–5 (onboard profiles)
- DPI — quick-set the current DPI, or edit any of the 4 presets the DPI button cycles through
- Lighting — effect (Static, Breathing, Colour cycle, Rainbow wave, Reactive, Battery level),
  brightness, colour picker
- Buttons — remap any button except left click; "Restore default buttons"
- Performance — button response (debounce 4–32 ms), angle snapping
- Tray settings — battery check interval, low-battery alert threshold

Every change is written to the mouse's onboard memory, so it also applies on other computers.

**Notifications** — low battery (your threshold, plus a critical one at 5%), fully charged.

**CLI** — `rogmouse --once` (battery) / `rogmouse --all` (all settings).

## Install

Requires Python 3.10+ and a desktop with a StatusNotifier tray (tested on Pop!_OS 24.04 / COSMIC).

    git clone https://github.com/KillerbobSE/RogMouse.git && cd RogMouse
    pipx install .                     # or: python3 -m venv .venv && .venv/bin/pip install .
    sudo cp 70-rogmouse.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger
    cp rogmouse.desktop ~/.local/share/applications/   # app launcher entry
    cp rogmouse.desktop ~/.config/autostart/           # start at login

The desktop file runs `rogmouse` from your `PATH` (pipx puts it in `~/.local/bin`). With a plain
venv, change `Exec=` to the full path of `.venv/bin/rogmouse`.

## Supported hardware

Tested with the **ROG Keris II Origin** over the **ROG Omni Receiver** (`0b05:1ace`).
Other ROG mice behind the Omni Receiver probably speak the same protocol but have different
buttons/DPI ranges — untested. Wired mode (`0b05:1c0c`) and Bluetooth are untested/unsupported.

## Protocol

Documented at the top of `src/rogmouse/protocol.py`. Every command there was verified on the
device (write → read back → restore). Not implemented: polling rate (encoding unknown on this
model), sleep timer, keyboard-key/macro button bindings. Wired mode (0b05:1c0c) is untested.

## COSMIC notes

- COSMIC's tray only expands one submenu level and keeps the dbusmenu layout it fetched, so
  menu actions are created once and updated in place (recreating them silently breaks clicks).
- If tray icons vanish after a panel crash: `pkill -x cosmic-panel`, then restart the tray apps.
  Don't kill a single `cosmic-applet-status-area` — the panel won't respawn it.
- Launching RogMouse while it's already running just exits (single instance).
- Follows the desktop light/dark setting via xdg-desktop-portal. If `QT_QPA_PLATFORMTHEME` names
  a theme Qt 6 can't load (e.g. `qt5ct`), the built-in portal theme is used instead.

## Credits

The ASUS command set comes from [libratbag](https://github.com/libratbag/libratbag)'s `asus`
driver and its Keris II Origin work ([PR #1776](https://github.com/libratbag/libratbag/pull/1776));
battery/charging fields and the Keris II Origin layout were worked out on the device.

Not affiliated with or endorsed by ASUS. ROG, Keris and Armoury Crate are trademarks of ASUSTeK.

## License

MIT — see [LICENSE](LICENSE).
