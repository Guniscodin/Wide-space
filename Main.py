"""
WideSpace - turns every space you type into several spaces.
Helps people with dyslexia read text more easily.

Run:  python Main.py            (add --minimized to start out of the way)
Toggle on/off from the window, or with Ctrl + Alt + S anywhere.
Settings and log file live in  <APPDATA or ~/.config>/WideSpace
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pynput import keyboard

APP_NAME = "WideSpace"
IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

DEFAULT_SPACES = 3
MIN_SPACES, MAX_SPACES = 2, 10

INJECT_TTL = 0.25        # how long we remember keys we sent ourselves
GAP_WINDOW = 6.0         # Backspace removes a whole gap only this soon after it
TOGGLE_DEBOUNCE = 0.5    # ignore hotkey auto-repeat
BREAKER_BATCHES = 60     # safety stop: this many injections ...
BREAKER_WINDOW = 1.0     # ... within this many seconds means a runaway loop
INSTANCE_PORT = 47653    # used only to make sure one copy runs at a time

ICON_BASENAME = "icon"   # drop "icon.ico" (Windows) or "icon.png" (Linux) next to this file

log = logging.getLogger(APP_NAME)

# Keys that never change our state (holding Shift must not break a gap, etc.)
PASSIVE_KEYS = frozenset({
    keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
    keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr,
    keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r,
    keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r,
    keyboard.Key.caps_lock,
})
S_VKS = (0x53,) if IS_WIN else (0x53, 0x73)   # Windows vk / X11 keysym for "S"


# ---------------------------------------------------------------- settings --

def config_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def resource_path(relative: str) -> Path:
    """Return the path to a bundled resource, works for source and PyInstaller."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    return base / relative


def clamp(n: int) -> int:
    return max(MIN_SPACES, min(MAX_SPACES, n))


def load_spaces() -> int:
    try:
        data = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
        return clamp(int(data["spaces"]))
    except Exception:
        return DEFAULT_SPACES


def save_spaces(n: int) -> None:
    try:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "config.json.tmp"
        tmp.write_text(json.dumps({"spaces": n}), encoding="utf-8")
        tmp.replace(d / "config.json")     # atomic: never a half-written file
    except Exception:
        log.exception("could not save settings")


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    try:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(d / "log.txt", maxBytes=200_000, backupCount=1,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    except Exception:
        log.addHandler(logging.NullHandler())


# ------------------------------------------------- Windows start-at-login --

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    return f'"{runner}" "{Path(__file__).resolve()}" --minimized'


def get_autostart() -> bool:
    if not IS_WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ------------------------------------------------------- one copy at a time --

_instance_sock = None


def acquire_instance() -> bool:
    global _instance_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", INSTANCE_PORT))
    except OSError:
        s.close()
        return False
    _instance_sock = s   # keep alive for the life of the process
    return True


# ---------------------------------------------------------- OS back-ends ----
# Each back-end can (1) tap a key n times quickly, and (2) ask the OS which
# modifiers are physically held right now (more reliable than tracking
# press/release events, which can be missed).

class WindowsBackend:
    def __init__(self) -> None:
        self._c = keyboard.Controller()
        self._gas = ctypes.windll.user32.GetAsyncKeyState
        self._gas.restype = ctypes.c_short
        self._gas.argtypes = [ctypes.c_int]

    def send(self, key, n: int) -> None:
        for _ in range(n):
            self._c.press(key)
            self._c.release(key)

    def mods(self) -> set:
        g = self._gas
        m = set()
        if g(0x11) & 0x8000:
            m.add("ctrl")
        if g(0x12) & 0x8000:
            m.add("alt")
        if g(0x5B) & 0x8000 or g(0x5C) & 0x8000:
            m.add("win")
        return m


class LinuxBackend:
    def __init__(self) -> None:
        from Xlib import X, XK, display
        from Xlib.ext import xtest
        self._X, self._xtest = X, xtest
        self._d = display.Display()
        self._root = self._d.screen().root
        self._codes = {
            keyboard.Key.space: self._d.keysym_to_keycode(XK.XK_space),
            keyboard.Key.backspace: self._d.keysym_to_keycode(XK.XK_BackSpace),
        }

    def send(self, key, n: int) -> None:
        code = self._codes[key]
        # The physical key is usually still held down when we get here, and X
        # ignores a second press of a key that is already down. Release it
        # first so that every injected press really counts.
        self._xtest.fake_input(self._d, self._X.KeyRelease, code)
        for _ in range(n):
            self._xtest.fake_input(self._d, self._X.KeyPress, code)
            self._xtest.fake_input(self._d, self._X.KeyRelease, code)
        self._d.sync()     # one round-trip for the whole batch

    def mods(self) -> set:
        mask = self._root.query_pointer().mask
        m = set()
        if mask & self._X.ControlMask:
            m.add("ctrl")
        if mask & self._X.Mod1Mask:
            m.add("alt")
        if mask & self._X.Mod4Mask:
            m.add("win")
        return m


def make_backend():
    if IS_WIN:
        return WindowsBackend()
    if IS_LINUX:
        return LinuxBackend()
    raise RuntimeError("Only Windows and Linux (X11) are supported for now.")


def _is_s(key) -> bool:
    ch = getattr(key, "char", None)
    if ch and ch.lower() == "s":
        return True
    return getattr(key, "vk", None) in S_VKS


# ---------------------------------------------------------- the engine ------

class WideSpace:
    def __init__(self, n_spaces: int, backend=None) -> None:
        self.n_spaces = clamp(n_spaces)
        self.enabled = True
        self.tripped = False        # True after the runaway safety stop fired
        self.restarts = 0

        self._backend = backend or make_backend()
        self._injected: deque = deque()                      # (key, time)
        self._batches: deque = deque(maxlen=BREAKER_BATCHES)  # injection times
        self._gap_until = 0.0
        self._last_toggle = 0.0
        self._next_restart = 0.0
        self._started_at = 0.0
        self._listener: keyboard.Listener | None = None

    # -- state changes
    def toggle(self) -> None:
        self.enabled = not self.enabled
        self.tripped = False
        self._reset()
        log.info("turned %s", "ON" if self.enabled else "OFF")

    def _reset(self) -> None:
        self._injected.clear()
        self._gap_until = 0.0

    # -- injecting our own keys
    def _inject(self, key, n: int) -> bool:
        if n <= 0:
            return True
        now = time.monotonic()
        self._batches.append(now)
        if (len(self._batches) == self._batches.maxlen
                and now - self._batches[0] < BREAKER_WINDOW):
            self.enabled = False
            self.tripped = True
            self._reset()
            log.error("safety stop: runaway key injection, switched OFF")
            return False
        for _ in range(n):
            self._injected.append((key, now))
        self._backend.send(key, n)
        return True

    def _consume_injected(self, key) -> bool:
        now = time.monotonic()
        # Drop anything that's older than the TTL from the front.
        while self._injected and now - self._injected[0][1] > INJECT_TTL:
            self._injected.popleft()
        # Search the whole remaining queue, not just the front, so a stale
        # injected Space can't misclassify a real Backspace (or vice versa).
        for i, (k, t) in enumerate(self._injected):
            if now - t <= INJECT_TTL and k == key:
                del self._injected[i]
                return True
        return False

    # -- key handling
    def _on_press(self, key) -> None:
        # Never let an exception escape: pynput would silently kill the listener.
        try:
            self._handle_press(key)
        except Exception:
            log.exception("key handler failed")
            self._reset()

    def _handle_press(self, key) -> None:
        if self._consume_injected(key):
            return
        if key in PASSIVE_KEYS:
            return
        now = time.monotonic()

        if _is_s(key):
            if {"ctrl", "alt"} <= self._backend.mods():
                if now - self._last_toggle > TOGGLE_DEBOUNCE:
                    self._last_toggle = now
                    self.toggle()
                return
            self._gap_until = 0.0
            return

        if key == keyboard.Key.space:
            if not self.enabled or self._backend.mods():
                self._gap_until = 0.0
                return
            if self._inject(keyboard.Key.space, self.n_spaces - 1):
                self._gap_until = now + GAP_WINDOW
            return

        if key == keyboard.Key.backspace:
            if self.enabled and now < self._gap_until and not self._backend.mods():
                self._gap_until = 0.0
                self._inject(keyboard.Key.backspace, self.n_spaces - 1)
            else:
                self._gap_until = 0.0
            return

        self._gap_until = 0.0

    # -- listener life-cycle (with a watchdog)
    @property
    def hook_ok(self) -> bool:
        l = self._listener
        if l is None or not l.is_alive():
            return False
        # `running` turns False when pynput stops the listener (e.g. after an
        # error) even though its thread may linger for a moment.
        return l.running or time.monotonic() - self._started_at < 2.0

    def start(self) -> None:
        self.stop()
        self._started_at = time.monotonic()
        try:
            self._listener = keyboard.Listener(on_press=self._on_press)
            self._listener.start()
        except Exception:
            log.exception("could not start keyboard listener")
            self._listener = None

    def stop(self) -> None:
        l, self._listener = self._listener, None
        if l is not None:
            try:
                l.stop()
            except Exception:
                pass

    def ensure_running(self) -> None:
        """Called regularly by the UI: restart the hook if it ever dies."""
        if self.hook_ok:
            return
        now = time.monotonic()
        if now < self._next_restart:
            return
        self._next_restart = now + 2.0
        self.restarts += 1
        log.warning("keyboard hook not running, restarting (#%d)", self.restarts)
        self._reset()
        self.start()


# ---------------------------------------------------------------- the UI ----

def show_error(message: str) -> None:
    # In PyInstaller --windowed builds sys.stderr can be None. Guard against it.
    try:
        if sys.stderr is not None:
            print(f"{APP_NAME}: {message}", file=sys.stderr)
    except Exception:
        pass

    log.error(message)
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, message)
        r.destroy()
    except Exception:
        pass


def run_gui(ws: WideSpace, start_minimized: bool) -> None:
    import tkinter as tk
    from tkinter import ttk

    # Soft dark theme: off-white on charcoal (pure white on black is harsher
    # to read, which matters for the dyslexia use case).
    BG, CARD, TEXT, MUTED = "#16181d", "#20242b", "#e6e8eb", "#8b929c"
    LINE = "#2f343d"                          # subtle borders
    GREEN, RED, GRAY = "#3ccf85", "#f0786c", "#3a3f47"
    FONT = "Verdana" if IS_WIN else "DejaVu Sans"   # wide, clear letterforms

    root = tk.Tk()
    root.title(APP_NAME)
    root.configure(bg=BG)
    root.resizable(False, False)
    k = max(1.0, root.winfo_fpixels("1i") / 96.0)   # HiDPI scale factor

    # --- window / taskbar icon
    # Windows uses .ico, Linux uses .png. Both are optional: if the file is
    # missing we just skip it silently.
    def set_window_icon() -> None:
        try:
            if IS_WIN:
                ico = resource_path(f"{ICON_BASENAME}.ico")
                if ico.exists():
                    root.iconbitmap(default=str(ico))
            else:
                png = resource_path(f"{ICON_BASENAME}.png")
                if png.exists():
                    img = tk.PhotoImage(file=str(png))
                    root.iconphoto(True, img)
                    root._icon_img = img   # keep a reference; Tk won't hold it
        except Exception:
            log.exception("could not set window icon")

    set_window_icon()

    # --- dark title bar (Windows 10/11); does nothing elsewhere
    def dark_titlebar() -> None:
        if not IS_WIN:
            return
        try:
            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):   # 20 = Win10 20H1+ / Win11, 19 = older Win10
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                    break
        except Exception:
            log.exception("could not set dark title bar")

    dark_titlebar()

    def px(v: float) -> int:
        return int(v * k)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG)
    style.configure("TButton", font=(FONT, 10), padding=(px(14), px(6)),
                    background=CARD, foreground=TEXT, bordercolor=LINE,
                    lightcolor=CARD, darkcolor=CARD, focuscolor=CARD)
    style.map("TButton", background=[("active", LINE), ("pressed", LINE)])
    style.configure("TCheckbutton", background=BG, foreground=TEXT, font=(FONT, 10),
                    indicatorbackground=CARD, indicatorforeground=GREEN,
                    upperbordercolor=LINE, lowerbordercolor=LINE, focuscolor=BG)
    style.map("TCheckbutton", background=[("active", BG)],
              indicatorbackground=[("active", CARD)])
    style.configure("Horizontal.TScale", background=GREEN, troughcolor=CARD,
                    bordercolor=LINE, lightcolor=GREEN, darkcolor=GREEN)

    body = tk.Frame(root, bg=BG, padx=px(24), pady=px(20))
    body.pack(fill="both", expand=True)

    tk.Label(body, text="WideSpace", font=(FONT, 20, "bold"),
             bg=BG, fg=TEXT).pack(anchor="w")
    tk.Label(body, text="Wider gaps between words make reading easier.",
             font=(FONT, 10), bg=BG, fg=MUTED).pack(anchor="w", pady=(0, px(14)))

    # --- on/off switch
    row = tk.Frame(body, bg=BG)
    row.pack(fill="x")
    SW_W, SW_H = px(84), px(42)
    canvas = tk.Canvas(row, width=SW_W, height=SW_H, bg=BG,
                       highlightthickness=0, cursor="hand2")
    canvas.pack(side="left")
    canvas.bind("<Button-1>", lambda e: ws.toggle())
    drawn = {"on": None}

    def draw_switch(on: bool) -> None:
        if drawn["on"] == on:
            return
        drawn["on"] = on
        canvas.delete("all")
        col = GREEN if on else GRAY
        r = SW_H // 2
        canvas.create_oval(0, 0, SW_H, SW_H, fill=col, outline=col)
        canvas.create_oval(SW_W - SW_H, 0, SW_W, SW_H, fill=col, outline=col)
        canvas.create_rectangle(r, 0, SW_W - r, SW_H, fill=col, outline=col)
        m = px(4)
        kx = SW_W - SW_H + m if on else m
        canvas.create_oval(kx, m, kx + SW_H - 2 * m, SW_H - m,
                           fill=TEXT, outline=TEXT)
        canvas.create_text(px(24) if on else SW_W - px(26), SW_H // 2,
                           text="ON" if on else "OFF",
                           fill="#0d2416" if on else "#c5c9cf",
                           font=(FONT, 9, "bold"))

    state_lbl = tk.Label(row, font=(FONT, 14, "bold"), bg=BG)
    state_lbl.pack(side="left", padx=(px(14), 0))

    # --- width slider + live preview
    tk.Label(body, text="How wide should the gaps be?", font=(FONT, 11, "bold"),
             bg=BG, fg=TEXT).pack(anchor="w", pady=(px(10), px(4)))
    srow = tk.Frame(body, bg=BG)
    srow.pack(fill="x")
    scale = ttk.Scale(srow, from_=MIN_SPACES, to=MAX_SPACES,
                      orient="horizontal", length=px(280))
    scale.pack(side="left")
    num = tk.Label(srow, font=(FONT, 16, "bold"), bg=BG, fg=TEXT, width=3)
    num.pack(side="left", padx=(px(12), 0))

    preview = tk.Label(body, bg=CARD, fg=TEXT, font=(FONT, 15), anchor="w",
                       justify="left", padx=px(16), pady=px(12), relief="flat",
                       bd=0, highlightthickness=1, highlightbackground=LINE,
                       wraplength=px(340), height=2)
    preview.pack(fill="x", pady=(px(10), 0))

    ui = {"save_job": None, "snap": False}

    def render(n: int) -> None:
        num.config(text=str(n))
        sp = " " * n
        preview.config(text=f"Easier{sp}to{sp}read")

    def on_scale(v: str) -> None:
        if ui["snap"]:
            return
        n = clamp(int(round(float(v))))
        if abs(float(v) - n) > 0.001:       # snap the slider to whole numbers
            ui["snap"] = True
            try:
                scale.set(n)
            finally:
                ui["snap"] = False
        if n != ws.n_spaces:
            ws.n_spaces = n
            render(n)
            if ui["save_job"]:
                root.after_cancel(ui["save_job"])
            ui["save_job"] = root.after(600, lambda: save_spaces(ws.n_spaces))

    scale.set(ws.n_spaces)
    render(ws.n_spaces)
    scale.configure(command=on_scale)

    # --- try-it box
    tk.Label(body, text="Try it here", font=(FONT, 10),
             bg=BG, fg=MUTED).pack(anchor="w", pady=(px(16), px(4)))
    box = tk.Text(body, width=36, height=3, wrap="word", font=(FONT, 13),
                  bg=CARD, fg=TEXT, insertbackground=TEXT, selectbackground=LINE,
                  selectforeground=TEXT, relief="flat", bd=0,
                  highlightthickness=1, highlightbackground=LINE,
                  highlightcolor=GREEN, padx=8, pady=6)
    box.pack(fill="x")

    # --- start at login (Windows only)
    status = tk.Label(body, font=(FONT, 9), bg=BG, fg=MUTED, anchor="w",
                      justify="left", wraplength=px(340))

    if IS_WIN:
        auto_var = tk.BooleanVar(value=get_autostart())

        def on_auto() -> None:
            try:
                set_autostart(auto_var.get())
            except Exception:
                log.exception("could not change start-at-login")
                auto_var.set(get_autostart())
                status.config(text="Could not change the start-at-login setting.",
                              fg=RED)

        ttk.Checkbutton(body, text="Start WideSpace when I sign in",
                        variable=auto_var, command=on_auto).pack(
            anchor="w", pady=(px(14), 0))

    # --- footer
    foot = tk.Frame(body, bg=BG)
    foot.pack(fill="x", pady=(px(14), 0))
    tk.Label(foot, text="Shortcut: Ctrl + Alt + S", font=(FONT, 9),
             bg=BG, fg=MUTED).pack(side="left")

    def quit_app() -> None:
        try:
            save_spaces(ws.n_spaces)
        finally:
            ws.stop()
            root.destroy()

    ttk.Button(foot, text="Quit", command=quit_app).pack(side="right")
    status.pack(anchor="w", pady=(px(6), 0), after=row)   # right under the switch
    tk.Label(body, font=(FONT, 8), bg=BG, fg=MUTED, justify="left",
             wraplength=px(340),
             text="Closing the window keeps WideSpace running. "
                  "Quit stops it.").pack(anchor="w", pady=(px(6), 0))

    # --- refresh loop: never dies, also runs the keyboard-hook watchdog
    def tick() -> None:
        try:
            ws.ensure_running()
            on = ws.enabled
            draw_switch(on)
            state_lbl.config(text="Wide spaces ON" if on else "Wide spaces OFF",
                             fg=GREEN if on else RED)
            if ws.tripped:
                status.config(fg=RED, text="Paused for safety: too many keys were "
                              "sent at once. Switch it back on to continue.")
            elif not ws.hook_ok:
                status.config(fg=RED, text="Keyboard hook stopped, restarting...")
            elif status.cget("text").startswith(("Paused", "Keyboard hook")):
                status.config(fg=MUTED, text="")
        except Exception:
            log.exception("ui refresh failed")
        finally:
            root.after(250, tick)

    root.protocol("WM_DELETE_WINDOW", root.iconify)
    tick()
    if start_minimized:
        root.iconify()
    root.mainloop()


def main() -> int:
    setup_logging()
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp on HiDPI
        except Exception:
            pass
    try:
        import tkinter  # noqa: F401
    except ImportError:
        show_error("tkinter is missing. On Linux run: sudo apt install python3-tk")
        return 1

    if not acquire_instance():
        show_error("WideSpace is already running. Look for it in your taskbar.")
        return 0

    try:
        ws = WideSpace(load_spaces())
    except Exception as exc:
        log.exception("startup failed")
        hint = ""
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            hint = "\n\nYou are on Wayland. Log in with an X11/Xorg session instead."
        show_error(f"Could not start the keyboard hook: {exc}{hint}")
        return 1

    ws.start()
    log.info("started, %d spaces", ws.n_spaces)
    try:
        run_gui(ws, "--minimized" in sys.argv)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.exception("fatal UI error")
        show_error(f"Something went wrong: {exc}")
        return 1
    finally:
        ws.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())