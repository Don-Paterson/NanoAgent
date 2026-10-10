"""
nano_client.py - synchronous controller library for NanoAgent.

    from nano_client import NanoClient
    with NanoClient.from_config("controller.json") as a:
        a.wait_for_agent()
        a.focus_window("SmartConsole")
        a.click(120, 85)
        a.click_image("anchors/publish.png", timeout=15)   # wait for a popup, then click it
        a.type("Demo session")
        a.key("Return")
        img = a.screenshot("after.png")
        rc, out, err = a.run("Get-Service CP*")

All coordinates are screenshot pixels (as returned by screenshot()).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from nano_proto import PubSubLink, key_from_secret, make_cmd, support_session, verify

log = logging.getLogger("nano.client")


class NanoError(RuntimeError):
    pass


class NanoClient:
    def __init__(self, negotiate_url: str, group: str, key: bytes, timeout: float = 30):
        self.group = group
        self._key = key
        self.timeout = timeout
        self.agent_info: Optional[dict] = None
        self._hello = threading.Event()
        self._pending: dict[str, asyncio.Future] = {}
        self._loop = asyncio.new_event_loop()
        self._stop: Optional[asyncio.Event] = None
        self._link = PubSubLink(negotiate_url, "controller", group, key, self._on_data)
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="nano-client")
        self._thread.start()
        self._ready.wait(5)

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_config(cls, path: str = "controller.json", support_code: Optional[str] = None, **kw):
        cfg = json.loads(Path(path).read_text(encoding="utf-8-sig")) if Path(path).exists() else {}
        url = os.environ.get("NANO_NEGOTIATE_URL") or cfg.get("negotiate_url")
        if not url:
            raise NanoError("negotiate_url missing (controller.json or NANO_NEGOTIATE_URL)")
        if support_code:
            group, key = support_session(support_code)
        else:
            group = os.environ.get("NANO_GROUP") or cfg.get("group")
            secret = os.environ.get("NANO_SECRET") or cfg.get("secret")
            if not group or not secret:
                raise NanoError("group and secret required in controller.json")
            key = key_from_secret(secret)
        return cls(url, group, key, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()
        self._ready.set()
        try:
            self._loop.run_until_complete(self._link.run_forever(self._stop))
        except Exception:
            log.exception("client loop ended")

    def close(self):
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=3)

    # ------------------------------------------------------------------ transport
    async def _on_data(self, data: dict):
        kind = data.get("kind")
        if kind not in ("res", "hello"):
            return
        if not verify(data, self._key):
            log.warning("dropping %s with bad signature", kind)
            return
        if kind == "hello":
            self.agent_info = data.get("info")
            self._hello.set()
            log.info("agent hello: %s", self.agent_info)
            return
        fut = self._pending.pop(data.get("id"), None)
        if fut and not fut.done():
            fut.set_result(data)

    async def _call(self, op: str, args: dict, timeout: float) -> dict:
        try:
            await asyncio.wait_for(self._link.connected.wait(), timeout)
        except asyncio.TimeoutError:
            raise NanoError("could not connect to the relay") from None
        msg = make_cmd(op, args, self._key)
        fut = self._loop.create_future()
        self._pending[msg["id"]] = fut
        try:
            await self._link.send(msg)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise NanoError(f"{op}: no reply within {timeout}s (agent offline, or wrong secret?)") from None
        finally:
            self._pending.pop(msg["id"], None)

    def call(self, op: str, _timeout: Optional[float] = None, **args) -> dict:
        """Send one command and return its result dict (raises NanoError on failure).
        _timeout is how long to wait for the reply; everything else goes to the agent."""
        t = _timeout or self.timeout
        res = asyncio.run_coroutine_threadsafe(self._call(op, args, t), self._loop).result(t + 10)
        if not res.get("ok"):
            raise NanoError(f"{op}: {res.get('error')}")
        return res.get("result") or {}

    def wait_for_agent(self, timeout: float = 60) -> dict:
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                # short attempts: the agent may join the group a moment after we do
                self.agent_info = self.call("info", _timeout=min(4, max(1, end - time.time())))
                return self.agent_info
            except (NanoError, TimeoutError) as e:
                last = e
                time.sleep(1)
        raise NanoError(f"agent did not answer within {timeout}s: {last}")

    # ------------------------------------------------------------------ screen
    @staticmethod
    def _decode(image: dict) -> Image.Image:
        return Image.open(io.BytesIO(base64.b64decode(image["data"])))

    def screenshot_raw(self, format: str = "jpeg", quality: Optional[int] = None) -> dict:
        args = {"format": format}
        if quality is not None:
            args["quality"] = quality           # lower quality = fewer bytes = fewer billed messages
        return self.call("screenshot", **args)["image"]

    def screenshot(self, save: Optional[str] = None, quality: Optional[int] = None) -> Image.Image:
        img = self._decode(self.screenshot_raw(quality=quality))
        if save:
            img.save(save)
        return img

    def zoom(self, x0, y0, x1, y1, save: Optional[str] = None, quality: Optional[int] = None) -> Image.Image:
        args = {"region": [x0, y0, x1, y1]}
        if quality is not None:
            args["quality"] = quality
        img = self._decode(self.call("zoom", **args)["image"])
        if save:
            img.save(save)
        return img

    def cursor(self) -> tuple[int, int]:
        r = self.call("cursor_position")
        return r["x"], r["y"]

    # ------------------------------------------------------------------ mouse / keyboard
    def click(self, x=None, y=None, button: str = "left", mods: Optional[str] = None):
        op = {"left": "left_click", "right": "right_click", "middle": "middle_click",
              "double": "double_click", "triple": "triple_click"}[button]
        args = {}
        if x is not None:
            args["coordinate"] = [x, y]
        if mods:
            args["text"] = mods
        self.call(op, **args)

    def double_click(self, x, y):
        self.click(x, y, "double")

    def right_click(self, x, y):
        self.click(x, y, "right")

    def move(self, x, y):
        self.call("mouse_move", coordinate=[x, y])

    def drag(self, x1, y1, x2, y2, mods: Optional[str] = None):
        args = {"start_coordinate": [x1, y1], "coordinate": [x2, y2]}
        if mods:
            args["text"] = mods
        self.call("left_click_drag", **args)

    def scroll(self, x, y, direction: str = "down", amount: int = 3):
        self.call("scroll", coordinate=[x, y], scroll_direction=direction, scroll_amount=amount)

    def type(self, text: str, interval: float = 0.02):
        self.call("type", text=text, interval=interval,
                  _timeout=max(self.timeout, len(text) * (interval + 0.01) + 10))

    def key(self, combo: str, repeat: int = 1):
        self.call("key", text=combo, repeat=repeat)

    def wait(self, seconds: float):
        self.call("wait", duration=seconds, _timeout=seconds + 15)

    # ------------------------------------------------------------------ shell / windows / files
    def run(self, command: str, timeout: float = 60, shell: str = "auto") -> tuple[int, str, str]:
        r = self.call("run", _timeout=timeout + 15, command=command, timeout=timeout, shell=shell)
        return r["rc"], r["stdout"], r["stderr"]

    def windows(self) -> list[dict]:
        return self.call("windows")["windows"]

    def focus_window(self, title_regex: str) -> str:
        return self.call("focus_window", title=title_regex)["title"]

    def move_window(self, title_regex: str, x: int, y: int, w: int, h: int) -> str:
        return self.call("move_window", title=title_regex, rect=[x, y, w, h])["title"]

    CHUNK = 384 * 1024          # raw bytes per file transfer message (agent limit is 512 KB)

    def get_file(self, remote: str, local: str, tail: Optional[int] = None) -> int:
        """Fetch a file in chunks.  tail=N fetches only the last N bytes.  Returns bytes written.
        Each chunk is one Web PubSub message, so a big file costs many messages: see CLAUDE.md."""
        if tail is not None:
            r = self.call("file_get", path=remote, tail=tail)
            Path(local).write_bytes(base64.b64decode(r["data"]))
            return r["length"]
        r = self.call("file_get", path=remote, offset=0, length=self.CHUNK)
        total = r["size"]
        written = 0
        with open(local, "wb") as f:
            while True:
                chunk = base64.b64decode(r["data"])
                f.write(chunk)
                written += len(chunk)
                if r["eof"] or not chunk:
                    break
                r = self.call("file_get", path=remote, offset=written, length=self.CHUNK)
        if written != total:
            raise NanoError(f"file_get: got {written} of {total} bytes (file changed while reading?)")
        return written

    def put_file(self, local: str, remote: str, overwrite: bool = False) -> str:
        raw = Path(local).read_bytes()
        path = remote
        for i in range(0, max(len(raw), 1), self.CHUNK):
            data = base64.b64encode(raw[i:i + self.CHUNK]).decode("ascii")
            r = self.call("file_put", path=remote, data=data, overwrite=overwrite if i == 0 else False,
                          append=i > 0)
            path = r["path"]
        return path

    def file_sha256(self, remote: str) -> dict:
        return self.call("file_sha256", path=remote)

    # ------------------------------------------------------------------ jobs (long-running work)
    def job_start(self, script: Optional[str] = None, command: Optional[str] = None, shell: str = "pwsh",
                  timeout: Optional[int] = None, watch=None, name: Optional[str] = None) -> dict:
        args = {"shell": shell}
        for k, v in (("script", script), ("command", command), ("timeout", timeout), ("watch", watch), ("name", name)):
            if v is not None:
                args[k] = v
        return self.call("job_start", **args)

    def job_status(self, job_id: Optional[str] = None) -> list:
        return self.call("job_status", **({"id": job_id} if job_id else {}))["jobs"]

    def job_tail(self, job_id: str, lines: int = 40, watch: bool = True) -> dict:
        return self.call("job_tail", id=job_id, lines=lines, watch=watch)

    def job_kill(self, job_id: str) -> dict:
        return self.call("job_kill", id=job_id)

    def usage(self) -> dict:
        """Estimated Web PubSub messages (2 KB units) this client has sent and received."""
        return self._link.usage()

    # ------------------------------------------------------------------ image anchors (controller side)
    def find_image(self, template: str, threshold: float = 0.9,
                   shot: Optional[Image.Image] = None) -> Optional[tuple[int, int]]:
        """Centre of `template` in the current screenshot, or None.

        Capture templates from screenshots taken by this tool (nano_cli crop) so the
        scale matches.  Needs opencv-python and numpy."""
        try:
            import cv2
            import numpy as np
        except ImportError as e:
            raise NanoError("find_image needs: pip install opencv-python numpy") from e
        shot = shot or self.screenshot()
        hay = cv2.cvtColor(np.array(shot.convert("RGB")), cv2.COLOR_RGB2GRAY)
        needle = cv2.cvtColor(np.array(Image.open(template).convert("RGB")), cv2.COLOR_RGB2GRAY)
        res = cv2.matchTemplate(hay, needle, cv2.TM_CCOEFF_NORMED)
        _, best, _, loc = cv2.minMaxLoc(res)
        if best < threshold:
            return None
        h, w = needle.shape
        return loc[0] + w // 2, loc[1] + h // 2

    def wait_image(self, template: str, timeout: float = 15, threshold: float = 0.9,
                   poll: float = 0.7) -> tuple[int, int]:
        end = time.time() + timeout
        while True:
            pos = self.find_image(template, threshold)
            if pos:
                return pos
            if time.time() > end:
                raise NanoError(f"{template} not seen within {timeout}s")
            time.sleep(poll)

    def click_image(self, template: str, timeout: float = 15, threshold: float = 0.9,
                    button: str = "left") -> tuple[int, int]:
        x, y = self.wait_image(template, timeout, threshold)
        self.click(x, y, button)
        return x, y
