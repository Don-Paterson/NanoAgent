"""
nano_proto.py - shared protocol for NanoAgent (agent and controller).

Transport: Azure Web PubSub "json.webpubsub.azure.v1" subprotocol (or the
compatible local_relay.py).  Both ends join one group and exchange JSON
envelopes inside sendToGroup messages:

    command  {"kind":"cmd","v":1,"id":<uuid>,"ts":<epoch>,"op":<name>,"args":{...},"sig":<hmac>}
    result   {"kind":"res","v":1,"id":<same id>,"ts":..,"ok":true|false,"result":{...}|"error":"..","sig":..}
    hello    {"kind":"hello","v":1,"ts":..,"info":{...},"sig":..}

Every envelope is HMAC-SHA256 signed with a pre-shared secret, so holding a
Web PubSub token for the group is NOT enough to drive the agent.  Commands
older than MAX_SKEW seconds, or with a repeated id, are rejected.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from typing import Awaitable, Callable, Optional

import websockets

PROTO_VERSION = 1
SUBPROTOCOL = "json.webpubsub.azure.v1"
MAX_SKEW = 120          # seconds a signed command stays valid
MAX_WS_MESSAGE = 16 * 1024 * 1024

log = logging.getLogger("nano")


# --------------------------------------------------------------------------- signing
def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def key_from_secret(secret: str) -> bytes:
    """64 hex chars -> raw 32-byte key; anything else is hashed."""
    s = (secret or "").strip()
    if not s:
        raise ValueError("empty secret")
    if len(s) == 64:
        try:
            return bytes.fromhex(s)
        except ValueError:
            pass
    return hashlib.sha256(s.encode("utf-8")).digest()


def support_session(code: str) -> tuple[str, bytes]:
    """Map a spoken support code like '4821-9930' to (group, key)."""
    digits = "".join(c for c in code if c.isdigit())
    if len(digits) < 8:
        raise ValueError("support code needs 8 digits")
    return f"sup-{digits}", hashlib.sha256(f"nano-support:{digits}".encode()).digest()


def sign(msg: dict, key: bytes) -> dict:
    body = {k: v for k, v in msg.items() if k != "sig"}
    msg["sig"] = hmac.new(key, canonical(body), hashlib.sha256).hexdigest()
    return msg


def verify(msg: dict, key: bytes) -> bool:
    sig = msg.get("sig")
    if not isinstance(sig, str):
        return False
    body = {k: v for k, v in msg.items() if k != "sig"}
    expected = hmac.new(key, canonical(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def make_cmd(op: str, args: dict, key: bytes) -> dict:
    return sign({"kind": "cmd", "v": PROTO_VERSION, "id": uuid.uuid4().hex,
                 "ts": time.time(), "op": op, "args": args or {}}, key)


class ReplayGuard:
    """Rejects stale or repeated command ids."""

    def __init__(self, window: float = MAX_SKEW):
        self.window = window
        self.seen: dict[str, float] = {}

    def check(self, msg: dict) -> Optional[str]:
        now = time.time()
        ts = msg.get("ts")
        if not isinstance(ts, (int, float)) or abs(now - ts) > self.window:
            return "stale or missing timestamp (check clocks on both machines)"
        mid = msg.get("id")
        if not isinstance(mid, str) or mid in self.seen:
            return "missing or repeated id"
        self.seen[mid] = now
        if len(self.seen) > 5000:
            cutoff = now - self.window * 2
            self.seen = {k: v for k, v in self.seen.items() if v > cutoff}
        return None


# --------------------------------------------------------------------------- negotiate
def negotiate(negotiate_url: str, role: str, group: str, timeout: float = 20) -> str:
    """Ask the negotiate endpoint (Azure Function or local relay) for a client URL.

    Existing query parameters (e.g. ?code=<function key>) are preserved."""
    parts = urllib.parse.urlsplit(negotiate_url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    q.update({"role": role, "group": group})
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))
    req = urllib.request.Request(url, headers={"User-Agent": "NanoAgent/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # honours HTTPS_PROXY
        body = json.loads(r.read().decode("utf-8"))
    if "url" not in body:
        raise RuntimeError(f"negotiate returned no url: {body}")
    return body["url"]


# --------------------------------------------------------------------------- link
class PubSubLink:
    """Keeps one WebSocket to Web PubSub alive, joined to `group`.

    on_data(data) is awaited for every group message (already JSON-decoded).
    on_connect() is awaited after each successful (re)join."""

    def __init__(self, negotiate_url: str, role: str, group: str,
                 on_data: Callable[[dict], Awaitable[None]],
                 on_connect: Optional[Callable[[], Awaitable[None]]] = None):
        self.negotiate_url = negotiate_url
        self.role = role
        self.group = group
        self.on_data = on_data
        self.on_connect = on_connect
        self.ws = None
        self.connected = asyncio.Event()
        self._ack = 0
        self._send_lock = asyncio.Lock()

    async def run_forever(self, stop: asyncio.Event):
        backoff = 1.0
        while not stop.is_set():
            try:
                url = await asyncio.to_thread(negotiate, self.negotiate_url, self.role, self.group)
                async with websockets.connect(url, subprotocols=[SUBPROTOCOL],
                                              max_size=MAX_WS_MESSAGE, ping_interval=20,
                                              ping_timeout=20, open_timeout=20) as ws:
                    self.ws = ws
                    self._ack += 1
                    await ws.send(json.dumps({"type": "joinGroup", "group": self.group,
                                              "ackId": self._ack}))
                    reader = asyncio.create_task(self._read(ws))
                    stopper = asyncio.create_task(stop.wait())
                    done, _ = await asyncio.wait({reader, stopper},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    for t in (reader, stopper):
                        if t not in done:
                            t.cancel()
                    if reader in done and reader.exception():
                        raise reader.exception()
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network, auth, negotiate errors
                log.warning("link: %s: %s", type(e).__name__, e)
            finally:
                self.ws = None
                self.connected.clear()
            if not stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _read(self, ws):
        async for raw in ws:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            t = m.get("type")
            if t == "ack":
                if not m.get("success", True):
                    log.warning("link: ack failure %s", m.get("error"))
                elif not self.connected.is_set():
                    self.connected.set()
                    log.info("link: joined group %s as %s", self.group, self.role)
                    if self.on_connect:
                        asyncio.create_task(self.on_connect())
            elif t == "message" and m.get("group") == self.group:
                data = m.get("data")
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except ValueError:
                        continue
                if isinstance(data, dict):
                    try:
                        await self.on_data(data)
                    except Exception:
                        log.exception("link: handler failed")
            elif t == "system" and m.get("event") == "disconnected":
                log.warning("link: service disconnected: %s", m.get("message"))

    async def send(self, data: dict):
        ws = self.ws
        if ws is None or not self.connected.is_set():
            raise ConnectionError("not connected to relay")
        frame = json.dumps({"type": "sendToGroup", "group": self.group, "dataType": "json",
                            "data": data, "noEcho": True})
        async with self._send_lock:
            await ws.send(frame)
