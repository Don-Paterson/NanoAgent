#!/usr/bin/env python3
"""
local_relay.py - a tiny Azure Web PubSub look-alike for LAN use and testing.

Implements just enough of the json.webpubsub.azure.v1 subprotocol (joinGroup,
leaveGroup, sendToGroup with noEcho, acks) plus a /negotiate endpoint, so the
agent and controller work unchanged against it.

    python local_relay.py --port 8765            # prints the negotiate URL to use
    python local_relay.py --port 8765 --key mysecretkey

Use it on a lab network (e.g. on the Skillable host side) or for testing.
It is plain ws:// - put it behind TLS, or use Azure, for anything over the internet.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from http import HTTPStatus

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

SUBPROTOCOL = "json.webpubsub.azure.v1"
log = logging.getLogger("relay")


class Relay:
    def __init__(self, negotiate_key: str, hub: str = "nano", token_minutes: int = 60):
        self.negotiate_key = negotiate_key
        self.hub = hub
        self.token_minutes = token_minutes
        self.signing_key = secrets.token_bytes(32)
        self.groups: dict[str, set] = {}

    # ------------------------------------------------------------------ tokens
    def _b64(self, b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    def make_token(self, sub: str, roles: list[str]) -> str:
        body = self._b64(json.dumps({"sub": sub, "roles": roles,
                                     "exp": time.time() + self.token_minutes * 60}).encode())
        sig = self._b64(hmac.new(self.signing_key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def check_token(self, tok: str):
        try:
            body, sig = tok.split(".")
            good = self._b64(hmac.new(self.signing_key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, good):
                return None
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            return claims if claims["exp"] > time.time() else None
        except Exception:
            return None

    # ------------------------------------------------------------------ HTTP side
    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        return Response(status.value, status.phrase,
                        Headers([("Content-Type", "application/json"), ("Content-Length", str(len(body)))]), body)

    def process_request(self, connection, request):
        url = urllib.parse.urlsplit(request.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        if url.path.rstrip("/") == "/negotiate":
            if not hmac.compare_digest(q.get("code", ""), self.negotiate_key):
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "bad code"})
            role, group = q.get("role", ""), q.get("group", "")
            if role not in ("agent", "controller") or not group or len(group) > 64 \
                    or not all(c.isalnum() or c in "-_" for c in group):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "role/group invalid"})
            tok = self.make_token(f"{role}-{secrets.token_hex(4)}",
                                  [f"webpubsub.joinLeaveGroup.{group}", f"webpubsub.sendToGroup.{group}"])
            host = request.headers.get("Host", "localhost")
            return self._json(HTTPStatus.OK, {"url": f"ws://{host}/client/hubs/{self.hub}?access_token={tok}"})
        if url.path.rstrip("/") == f"/client/hubs/{self.hub}":
            claims = self.check_token(q.get("access_token", ""))
            if not claims:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "bad or expired token"})
            connection.nano_claims = claims
            return None                                       # continue WebSocket handshake
        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    # ------------------------------------------------------------------ WebSocket side
    async def handler(self, ws):
        claims = ws.nano_claims
        roles, user = set(claims["roles"]), claims["sub"]
        conn_id = secrets.token_hex(8)
        joined: set[str] = set()
        log.info("connect %s (%s)", user, conn_id)
        await ws.send(json.dumps({"type": "system", "event": "connected", "userId": user,
                                  "connectionId": conn_id}))
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                t, g, ack = m.get("type"), m.get("group", ""), m.get("ackId")
                ok, err = True, None
                if t in ("joinGroup", "leaveGroup"):
                    if f"webpubsub.joinLeaveGroup.{g}" in roles:
                        if t == "joinGroup":
                            self.groups.setdefault(g, set()).add(ws)
                            joined.add(g)
                        else:
                            self.groups.get(g, set()).discard(ws)
                            joined.discard(g)
                    else:
                        ok, err = False, "Forbidden"
                elif t == "sendToGroup":
                    if f"webpubsub.sendToGroup.{g}" in roles:
                        out = json.dumps({"type": "message", "from": "group", "fromUserId": user, "group": g,
                                          "dataType": m.get("dataType", "json"), "data": m.get("data")})
                        for peer in list(self.groups.get(g, ())):
                            if peer is ws and m.get("noEcho"):
                                continue
                            try:
                                await peer.send(out)
                            except websockets.ConnectionClosed:
                                self.groups[g].discard(peer)
                    else:
                        ok, err = False, "Forbidden"
                if ack is not None:
                    a = {"type": "ack", "ackId": ack, "success": ok}
                    if err:
                        a["error"] = {"name": err, "message": f"{t} to {g} not permitted"}
                    await ws.send(json.dumps(a))
        except websockets.ConnectionClosed:
            pass
        finally:
            for g in joined:
                self.groups.get(g, set()).discard(ws)
            log.info("disconnect %s", user)


async def amain(host, port, key):
    relay = Relay(key)
    async with serve(relay.handler, host, port, subprotocols=[SUBPROTOCOL],
                     process_request=relay.process_request, max_size=16 * 1024 * 1024,
                     ping_interval=20):
        shown = "localhost" if host in ("0.0.0.0", "::") else host
        print(f"NanoAgent local relay on ws://{host}:{port}\n"
              f"negotiate_url: http://{shown}:{port}/negotiate?code={key}", flush=True)
        await asyncio.Future()


def main(argv=None):
    ap = argparse.ArgumentParser(description="NanoAgent local relay")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--key", default=None, help="negotiate key (random if omitted)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(amain(a.host, a.port, a.key or secrets.token_urlsafe(16)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
