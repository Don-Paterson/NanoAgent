#!/usr/bin/env python3
"""
nano_agent.py - NanoAgent remote-control agent.

Runs on the controlled machine (e.g. A-GUI).  Makes ONE outbound WebSocket to
Azure Web PubSub (or local_relay.py), joins a group and executes signed
commands: screenshots, mouse/keyboard (the same actions as Claude's computer
use toolset), PowerShell, window management and small file transfers.

    python nano_agent.py                       # uses agent.json next to the script
    python nano_agent.py --config C:\\NanoAgent\\agent.json
    python nano_agent.py --support             # one-off support session, prints a code

Kill switch: Ctrl+Alt+Shift+K (Windows), or close the console.
Coordinates in every command are in SCREENSHOT pixels; the agent scales them.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_WIN = os.name == "nt"
if IS_WIN:  # must happen before mss/pyautogui touch the screen
    import ctypes
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import mss
import pyautogui
from PIL import Image

from nano_proto import (PROTO_VERSION, PubSubLink, ReplayGuard, key_from_secret, sign,
                        support_session, verify)

__version__ = "1.0.0"
pyautogui.FAILSAFE = False      # Claude may legitimately click (0,0); use the kill hotkey instead
pyautogui.PAUSE = 0.03
log = logging.getLogger("nano.agent")
HERE = Path(__file__).resolve().parent
MAX_IMAGE_BYTES = 700_000       # stay well under the Web PubSub 1 MB message limit after base64
MAX_FILE_BYTES = 512 * 1024


# =========================================================================== screen
class Screen:
    """Screenshot capture plus the screenshot<->screen coordinate mapping."""

    def __init__(self, monitor: int = 1, max_long_edge: int = 1280):
        self.monitor_index = monitor
        self.max_long_edge = max_long_edge
        self._tls = threading.local()
        self.refresh()

    def _mss(self):
        # mss objects are thread-bound on Windows: keep one per worker thread
        sct = getattr(self._tls, "sct", None)
        if sct is None:
            sct = self._tls.sct = mss.mss()
        return sct

    def refresh(self):
        mons = self._mss().monitors
        idx = self.monitor_index if 0 < self.monitor_index < len(mons) else 1
        m = mons[idx]
        self.left, self.top, self.width, self.height = m["left"], m["top"], m["width"], m["height"]
        self.scale = min(1.0, self.max_long_edge / max(self.width, self.height))
        self.shot_w = round(self.width * self.scale)
        self.shot_h = round(self.height * self.scale)

    def to_screen(self, xy) -> tuple[int, int]:
        x, y = xy
        if not (0 <= x <= self.shot_w and 0 <= y <= self.shot_h):
            raise ValueError(f"coordinate {list(xy)} outside screenshot {self.shot_w}x{self.shot_h}")
        return self.left + round(x / self.scale), self.top + round(y / self.scale)

    def to_shot(self, sx, sy) -> list[int]:
        return [round((sx - self.left) * self.scale), round((sy - self.top) * self.scale)]

    def _grab(self, left, top, width, height) -> Image.Image:
        raw = self._mss().grab({"left": left, "top": top, "width": width, "height": height})
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def screenshot(self) -> Image.Image:
        self.refresh()
        img = self._grab(self.left, self.top, self.width, self.height)
        if self.scale < 1.0:
            img = img.resize((self.shot_w, self.shot_h), Image.LANCZOS)
        return img

    def zoom(self, region) -> Image.Image:
        x0, y0, x1, y1 = region
        if x1 <= x0 or y1 <= y0:
            raise ValueError("zoom region must be [x0, y0, x1, y1] with x1>x0, y1>y0")
        sx0, sy0 = self.to_screen((x0, y0))
        sx1, sy1 = self.to_screen((x1, y1))
        img = self._grab(sx0, sy0, max(1, sx1 - sx0), max(1, sy1 - sy0))
        img.thumbnail((self.shot_w, self.shot_h), Image.LANCZOS)   # fit, keep aspect
        return img

    @staticmethod
    def encode(img: Image.Image, fmt: str = "jpeg", quality: int = 70) -> dict:
        w, h = img.size
        work = img
        while True:
            buf = io.BytesIO()
            if fmt == "png":
                work.save(buf, "PNG", optimize=True)
            else:
                work.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_IMAGE_BYTES:
                break
            if fmt == "png":
                fmt = "jpeg"                       # fall back to JPEG
            elif quality > 35:
                quality -= 10
            else:                                  # never resize here: coordinates must stay valid
                break
        return {"media_type": f"image/{fmt}", "data": base64.b64encode(data).decode("ascii"),
                "width": work.width, "height": work.height, "source_size": [w, h]}


# =========================================================================== input
KEY_ALIASES = {
    "return": "enter", "kp_enter": "enter", "escape": "esc", "page_up": "pageup", "prior": "pageup",
    "page_down": "pagedown", "next": "pagedown", "super": "win", "super_l": "win", "meta": "win",
    "cmd": "win", "windows": "win", "control": "ctrl", "control_l": "ctrl", "control_r": "ctrlright",
    "ctrl_l": "ctrl", "alt_l": "alt", "alt_r": "altright", "shift_l": "shift", "shift_r": "shiftright",
    "backspace": "backspace", "back_space": "backspace", "del": "delete", "ins": "insert",
    "menu": "apps", "print": "printscreen", "minus": "-", "plus": "+", "equal": "=", "comma": ",",
    "period": ".", "slash": "/", "backslash": "\\", "semicolon": ";", "apostrophe": "'",
    "grave": "`", "bracketleft": "[", "bracketright": "]", "space": "space", "caps_lock": "capslock",
}


def parse_keys(combo: str) -> list[str]:
    """'ctrl+shift+Escape' -> ['ctrl','shift','esc'] in pyautogui names."""
    if not combo:
        return []
    if combo == "+":
        return ["+"]
    parts = [p for p in re.split(r"\+(?!$)", combo.strip()) if p]
    out = []
    for p in parts:
        k = p.strip()
        low = k.lower()
        name = KEY_ALIASES.get(low, low)
        if len(k) == 1:
            name = k.lower() if k.isalpha() else k
        if name not in pyautogui.KEYBOARD_KEYS:
            raise ValueError(f"unknown key '{k}'")
        out.append(name)
    return out


class _Held:
    """Context manager holding modifier keys down (for click+ctrl etc.)."""

    def __init__(self, combo):
        self.keys = parse_keys(combo) if combo else []

    def __enter__(self):
        for k in self.keys:
            pyautogui.keyDown(k)

    def __exit__(self, *a):
        for k in reversed(self.keys):
            pyautogui.keyUp(k)


if IS_WIN:
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _SendInput = ctypes.windll.user32.SendInput
    _SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)

    def _send_unicode_unit(unit: int):
        down = INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, unit, 0x0004, 0, 0)))   # KEYEVENTF_UNICODE
        up = INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, unit, 0x0004 | 0x0002, 0, 0)))
        arr = (INPUT * 2)(down, up)
        _SendInput(2, arr, ctypes.sizeof(INPUT))

    def type_text(text: str, interval: float):
        """Unicode-safe typing via SendInput (works for any character, any layout)."""
        for ch in text:
            if ch == "\n":
                pyautogui.press("enter")
            elif ch == "\t":
                pyautogui.press("tab")
            elif ch == "\r":
                continue
            else:
                b = ch.encode("utf-16-le")
                for i in range(0, len(b), 2):
                    _send_unicode_unit(int.from_bytes(b[i:i + 2], "little"))
            if interval:
                time.sleep(interval)

    def wheel(direction: str, amount: int):
        flags, delta = (0x0800, 120) if direction in ("up", "down") else (0x01000, 120)
        sign_ = 1 if direction in ("up", "right") else -1
        ctypes.windll.user32.mouse_event(flags, 0, 0, sign_ * delta * amount, 0)
else:
    def type_text(text: str, interval: float):
        pyautogui.write(text, interval=interval)

    def wheel(direction: str, amount: int):
        if direction in ("up", "down"):
            pyautogui.scroll(amount if direction == "up" else -amount)
        else:
            pyautogui.hscroll(amount if direction == "right" else -amount)


# =========================================================================== windows
if IS_WIN:
    user32 = ctypes.windll.user32
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _windows():
        out = []

        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left < 2 or r.bottom - r.top < 2:
                return True
            out.append({"hwnd": int(hwnd), "title": buf.value, "rect": [r.left, r.top, r.right, r.bottom],
                        "minimized": bool(user32.IsIconic(hwnd))})
            return True

        user32.EnumWindows(_EnumProc(cb), 0)
        return out

    def _find(title_re: str):
        rx = re.compile(title_re, re.I)
        for w in _windows():
            if rx.search(w["title"]):
                return w
        raise LookupError(f"no visible window matches /{title_re}/")

    def _focus(hwnd: int):
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)                     # SW_RESTORE
        # Attach to the foreground thread so SetForegroundWindow is allowed
        # (avoids the 'press Alt' trick, which can open menus in the target app).
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_thread, False)


# =========================================================================== banner / hotkey
class Banner:
    """Small click-through, capture-excluded 'remote control active' strip (Windows)."""

    def __init__(self, text: str):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.85)
        self.var = tk.StringVar(value=text)
        tk.Label(self.root, textvariable=self.var, bg="#b3261e", fg="white",
                 font=("Segoe UI", 9, "bold"), padx=10, pady=2).pack()
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        self.root.geometry(f"+{(self.root.winfo_screenwidth() - w) // 2}+0")
        self.root.update()
        if IS_WIN:
            try:
                hwnd = int(self.root.wm_frame(), 16)        # the real top-level HWND
                ex = user32.GetWindowLongW(hwnd, -20)
                # LAYERED | TRANSPARENT (click-through) | TOOLWINDOW | NOACTIVATE
                user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)
                user32.SetWindowDisplayAffinity(hwnd, 0x11)   # WDA_EXCLUDEFROMCAPTURE (Win10 2004+)
            except Exception:
                pass

    def set(self, text: str):
        self.root.after(0, self.var.set, text)

    def run(self, stop_flag: threading.Event):
        def poll():
            if stop_flag.is_set():
                self.root.destroy()
            else:
                self.root.after(250, poll)
        poll()
        self.root.mainloop()


def start_kill_hotkey(on_kill):
    if not IS_WIN:
        return

    def loop():
        MOD = 0x0001 | 0x0002 | 0x0004 | 0x4000          # ALT|CTRL|SHIFT|NOREPEAT
        if not user32.RegisterHotKey(None, 0x4E41, MOD, ord("K")):
            log.warning("could not register Ctrl+Alt+Shift+K (in use?)")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:                       # WM_HOTKEY
                on_kill()
                return

    threading.Thread(target=loop, daemon=True, name="hotkey").start()


# =========================================================================== agent
class Agent:
    def __init__(self, cfg: dict, key: bytes, group: str, status_cb=None):
        self.cfg = cfg
        self.key = key
        self.group = group
        self.screen = Screen(cfg.get("monitor", 1), cfg.get("max_long_edge", 1280))
        self.guard = ReplayGuard()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.status_cb = status_cb or (lambda s: None)
        self.last_activity = time.time()
        self.link = PubSubLink(cfg["negotiate_url"], "agent", group, self.on_data, self.on_connect)

    # ---- transport callbacks
    async def on_connect(self):
        self.status_cb(f"NanoAgent: connected [{self.group}]  -  Ctrl+Alt+Shift+K to stop")
        await self.send({"kind": "hello", "v": PROTO_VERSION, "ts": time.time(), "info": self.info()})

    async def on_data(self, data: dict):
        if data.get("kind") != "cmd":
            return
        if not verify(data, self.key):
            log.warning("rejected command with bad signature (op=%s)", data.get("op"))
            return                       # never answer unauthenticated senders
        err = self.guard.check(data)
        if err:
            await self.reply(data.get("id"), False, error=err)
            return
        await self.queue.put(data)

    async def send(self, msg: dict):
        try:
            await self.link.send(sign(msg, self.key))
        except Exception as e:
            log.warning("send failed: %s", e)

    async def reply(self, mid, ok, result=None, error=None):
        msg = {"kind": "res", "v": PROTO_VERSION, "id": mid, "ts": time.time(), "ok": ok}
        if ok:
            msg["result"] = result or {}
        else:
            msg["error"] = str(error)
        await self.send(msg)

    async def worker(self):
        while True:
            cmd = await self.queue.get()
            op, args = cmd.get("op"), cmd.get("args") or {}
            self.last_activity = time.time()
            short = {k: (v if len(str(v)) < 120 else f"<{len(str(v))} chars>") for k, v in args.items()}
            log.info("cmd %s %s", op, short)
            self.status_cb(f"NanoAgent: {op}  [{self.group}]  -  Ctrl+Alt+Shift+K to stop")
            try:
                fn = getattr(self, f"op_{op}", None)
                if fn is None:
                    raise ValueError(f"unknown op '{op}'")
                result = await fn(**args)
                await self.reply(cmd["id"], True, result)
            except Exception as e:
                log.warning("op %s failed: %s", op, e)
                await self.reply(cmd["id"], False, error=f"{type(e).__name__}: {e}")

    def info(self) -> dict:
        self.screen.refresh()
        return {"agent": __version__, "host": socket.gethostname(), "user": os.environ.get("USERNAME") or os.environ.get("USER"),
                "os": platform.platform(), "python": platform.python_version(),
                "screen": [self.screen.width, self.screen.height],
                "screenshot": [self.screen.shot_w, self.screen.shot_h], "scale": self.screen.scale,
                "allow_run": bool(self.cfg.get("allow_run", True)),
                "allow_files": bool(self.cfg.get("allow_files", True))}

    @staticmethod
    async def _t(fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    # ---- ops: information
    async def op_ping(self):
        return {"pong": time.time()}

    async def op_info(self):
        return self.info()

    async def op_screenshot(self, format: str = "jpeg"):
        img = await self._t(self.screen.screenshot)
        return {"image": self.screen.encode(img, format, self.cfg.get("jpeg_quality", 70))}

    async def op_zoom(self, region, format: str = "jpeg"):
        img = await self._t(self.screen.zoom, region)
        return {"image": self.screen.encode(img, format, self.cfg.get("jpeg_quality", 70))}

    async def op_cursor_position(self):
        p = pyautogui.position()
        x, y = self.screen.to_shot(p.x, p.y)
        return {"x": x, "y": y, "text": f"X={x}, Y={y}"}

    # ---- ops: mouse (computer-use toolset members)
    def _click(self, coordinate, text, button, clicks):
        with _Held(text):
            if coordinate is not None:
                x, y = self.screen.to_screen(coordinate)
                pyautogui.click(x, y, clicks=clicks, interval=0.06, button=button)
            else:
                pyautogui.click(clicks=clicks, interval=0.06, button=button)

    async def op_left_click(self, coordinate=None, text=None):
        await self._t(self._click, coordinate, text, "left", 1); return {}

    async def op_right_click(self, coordinate=None, text=None):
        await self._t(self._click, coordinate, text, "right", 1); return {}

    async def op_middle_click(self, coordinate=None, text=None):
        await self._t(self._click, coordinate, text, "middle", 1); return {}

    async def op_double_click(self, coordinate=None, text=None):
        await self._t(self._click, coordinate, text, "left", 2); return {}

    async def op_triple_click(self, coordinate=None, text=None):
        await self._t(self._click, coordinate, text, "left", 3); return {}

    async def op_left_click_drag(self, start_coordinate, coordinate, text=None):
        def go():
            sx, sy = self.screen.to_screen(start_coordinate)
            ex, ey = self.screen.to_screen(coordinate)
            with _Held(text):
                pyautogui.moveTo(sx, sy)
                pyautogui.mouseDown()
                pyautogui.moveTo(ex, ey, duration=0.35)
                pyautogui.mouseUp()
        await self._t(go); return {}

    async def op_mouse_move(self, coordinate):
        x, y = self.screen.to_screen(coordinate)
        await self._t(pyautogui.moveTo, x, y); return {}

    async def op_left_mouse_down(self):
        await self._t(pyautogui.mouseDown); return {}

    async def op_left_mouse_up(self):
        await self._t(pyautogui.mouseUp); return {}

    async def op_scroll(self, scroll_direction, scroll_amount=3, coordinate=None, text=None):
        if scroll_direction not in ("up", "down", "left", "right"):
            raise ValueError("scroll_direction must be up/down/left/right")

        def go():
            if coordinate is not None:
                pyautogui.moveTo(*self.screen.to_screen(coordinate))
            with _Held(text):
                wheel(scroll_direction, int(scroll_amount))
        await self._t(go); return {}

    # ---- ops: keyboard
    async def op_type(self, text, interval: float = 0.01):
        await self._t(type_text, str(text), float(interval)); return {}

    async def op_key(self, text, repeat: int = 1):
        keys = parse_keys(text)
        repeat = max(1, min(int(repeat), 100))

        def go():
            for _ in range(repeat):
                if len(keys) == 1:
                    pyautogui.press(keys[0])
                else:
                    pyautogui.hotkey(*keys, interval=0.03)
        await self._t(go); return {}

    async def op_hold_key(self, text, duration):
        keys = parse_keys(text)
        duration = max(0.0, min(float(duration), 300.0))

        def go():
            for k in keys:
                pyautogui.keyDown(k)
            try:
                time.sleep(duration)
            finally:
                for k in reversed(keys):
                    pyautogui.keyUp(k)
        await self._t(go); return {}

    async def op_wait(self, duration):
        await asyncio.sleep(max(0.0, min(float(duration), 300.0))); return {}

    # ---- ops: shell
    async def op_run(self, command, timeout: float = 60, shell: str = "auto"):
        if not self.cfg.get("allow_run", True):
            raise PermissionError("run is disabled on this agent")
        timeout = max(1.0, min(float(timeout), 600.0))
        if shell == "auto":
            shell = "powershell" if IS_WIN else "bash"
        if shell == "powershell":
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
        elif shell == "cmd":
            argv = ["cmd.exe", "/d", "/c", command]
        else:
            argv = ["bash", "-lc", command]

        def go():
            kw = {"creationflags": 0x08000000} if IS_WIN else {}          # CREATE_NO_WINDOW
            try:
                r = subprocess.run(argv, capture_output=True, timeout=timeout, **kw)
                rc, out, err = r.returncode, r.stdout, r.stderr
            except subprocess.TimeoutExpired as e:
                rc, out, err = -1, e.stdout or b"", (e.stderr or b"") + f"\n[timed out after {timeout}s]".encode()
            dec = lambda b: b.decode("utf-8", "replace")
            return {"rc": rc, "stdout": dec(out)[-60000:], "stderr": dec(err)[-20000:]}
        return await self._t(go)

    # ---- ops: windows
    async def op_windows(self):
        if not IS_WIN:
            raise NotImplementedError("window ops are Windows-only")
        wins = await self._t(_windows)
        for w in wins:
            l, t, r, b = w["rect"]
            w["rect_shot"] = self.screen.to_shot(l, t) + self.screen.to_shot(r, b)
        return {"windows": wins}

    async def op_focus_window(self, title):
        if not IS_WIN:
            raise NotImplementedError("window ops are Windows-only")
        w = await self._t(_find, title)
        await self._t(_focus, w["hwnd"])
        return {"title": w["title"]}

    async def op_move_window(self, title, rect):
        """rect = [x, y, width, height] in screenshot pixels."""
        if not IS_WIN:
            raise NotImplementedError("window ops are Windows-only")
        w = await self._t(_find, title)
        x, y, cw, ch = rect
        sx, sy = self.screen.to_screen((x, y))
        sw, sh = round(cw / self.screen.scale), round(ch / self.screen.scale)

        def go():
            user32.ShowWindow(w["hwnd"], 9)
            user32.MoveWindow(w["hwnd"], sx, sy, sw, sh, True)
            _focus(w["hwnd"])
        await self._t(go)
        return {"title": w["title"]}

    # ---- ops: files
    def _file_check(self):
        if not self.cfg.get("allow_files", True):
            raise PermissionError("file transfer is disabled on this agent")

    async def op_file_get(self, path):
        self._file_check()
        p = Path(os.path.expandvars(path)).expanduser()
        data = await self._t(p.read_bytes)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"file is {len(data)} bytes; limit {MAX_FILE_BYTES}")
        return {"path": str(p), "size": len(data), "data": base64.b64encode(data).decode("ascii")}

    async def op_file_put(self, path, data, overwrite: bool = False):
        self._file_check()
        raw = base64.b64decode(data)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("file too large")
        p = Path(os.path.expandvars(path)).expanduser()
        if p.exists() and not overwrite:
            raise FileExistsError(f"{p} exists (pass overwrite=true)")
        p.parent.mkdir(parents=True, exist_ok=True)
        await self._t(p.write_bytes, raw)
        return {"path": str(p), "size": len(raw)}

    # ---- main
    async def run(self, stop: asyncio.Event, idle_exit_min: float = 0):
        tasks = [asyncio.create_task(self.link.run_forever(stop)), asyncio.create_task(self.worker())]
        if idle_exit_min:
            async def idle():
                while not stop.is_set():
                    await asyncio.sleep(10)
                    if time.time() - self.last_activity > idle_exit_min * 60:
                        log.info("idle for %s min - exiting", idle_exit_min)
                        stop.set()
            tasks.append(asyncio.create_task(idle()))
        await stop.wait()
        for t in tasks:
            t.cancel()


# =========================================================================== entry
def load_config(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="NanoAgent remote-control agent")
    ap.add_argument("--config", default=str(HERE / "agent.json"))
    ap.add_argument("--negotiate-url", help="override negotiate_url")
    ap.add_argument("--group", help="override group (e.g. lab-a-gui)")
    ap.add_argument("--secret", help="override shared secret (or env NANO_SECRET)")
    ap.add_argument("--support", action="store_true", help="one-off support session with a spoken code")
    ap.add_argument("--no-run", action="store_true", help="disable remote PowerShell/shell")
    ap.add_argument("--no-files", action="store_true", help="disable file transfer")
    ap.add_argument("--no-banner", action="store_true")
    ap.add_argument("--idle-exit", type=float, default=None, help="exit after N idle minutes")
    ap.add_argument("--log", default=str(HERE / "nano_agent.log"))
    a = ap.parse_args(argv)

    handlers = [logging.FileHandler(a.log, encoding="utf-8")]
    if sys.stderr is not None:                      # pythonw.exe has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    cfg = load_config(Path(a.config))
    if a.negotiate_url:
        cfg["negotiate_url"] = a.negotiate_url
    if a.no_run:
        cfg["allow_run"] = False
    if a.no_files:
        cfg["allow_files"] = False
    if not cfg.get("negotiate_url"):
        sys.exit("negotiate_url missing (agent.json or --negotiate-url)")

    idle = a.idle_exit
    if a.support:
        code = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
        group, key = support_session(code)
        idle = 30 if idle is None else idle
        print("\n" + "=" * 46 + f"\n   SUPPORT CODE:  {code}\n   Read this to your support contact.\n"
              "   Ctrl+Alt+Shift+K or close this window to end.\n" + "=" * 46 + "\n", flush=True)
    else:
        group = a.group or cfg.get("group")
        secret = a.secret or os.environ.get("NANO_SECRET") or cfg.get("secret")
        if not group or not secret:
            sys.exit("group and secret are required (agent.json, --group/--secret, or --support)")
        key = key_from_secret(secret)

    stop_flag = threading.Event()
    banner = None
    if IS_WIN and not a.no_banner:
        try:
            banner = Banner(f"NanoAgent: connecting [{group}]")
        except Exception as e:
            log.warning("banner unavailable: %s", e)

    def kill():
        log.warning("kill hotkey pressed - exiting")
        os._exit(0)
    start_kill_hotkey(kill)

    def run_loop():
        async def amain():
            stop = asyncio.Event()

            async def watch():
                while not stop_flag.is_set():
                    await asyncio.sleep(0.25)
                stop.set()
            asyncio.create_task(watch())
            agent = Agent(cfg, key, group, banner.set if banner else None)
            log.info("NanoAgent %s starting; group=%s screen=%sx%s -> %sx%s", __version__, group,
                     agent.screen.width, agent.screen.height, agent.screen.shot_w, agent.screen.shot_h)
            await agent.run(stop, idle or 0)
        try:
            asyncio.run(amain())
        finally:
            stop_flag.set()

    if banner:
        t = threading.Thread(target=run_loop, daemon=True, name="agent")
        t.start()
        try:
            banner.run(stop_flag)
        except KeyboardInterrupt:
            pass
        stop_flag.set()
        t.join(timeout=3)
    else:
        try:
            run_loop()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
