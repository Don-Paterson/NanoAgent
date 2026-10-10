#!/usr/bin/env python3
"""nano_session.py - persistent NanoAgent session for interactive use from a shell
(built for Claude sessions driving A-GUI; see CLAUDE.md).  One background process holds
the encrypted connection, so each command costs ~0.4 s instead of ~4 s.

    python nano_session.py serve &                       # once; holds the connection
    python nano_session.py shot [file]                   # screenshot (1280x720 jpeg)
    python nano_session.py zoom x0 y0 x1 y1 [file]       # full-res crop of a region
    python nano_session.py click x y [left|right|double]
    python nano_session.py type "text"      |  key ctrl+s [repeat]
    python nano_session.py run "PowerShell" [timeout]
    python nano_session.py op <op> '<json args>'         # any agent op (windows, scroll, file_get...)

Env: NANO_CONFIG (controller.json, default ./controller.json), NANO_DIR (work dir for the
socket and images, default ./nano_work).  Coordinates are screenshot pixels.
"""
import base64, json, os, socket, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CFG = os.environ.get("NANO_CONFIG", "controller.json")
WORK = os.path.abspath(os.environ.get("NANO_DIR", "nano_work"))
os.makedirs(WORK, exist_ok=True)
SOCK = os.path.join(WORK, "session.sock")
SHOT = os.path.join(WORK, "now.jpg")


def serve():
    from nano_client import NanoClient
    c = NanoClient.from_config(CFG, timeout=60)
    c.wait_for_agent(30)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    s = socket.socket(socket.AF_UNIX); s.bind(SOCK); s.listen(4)
    print("session ready", flush=True)
    while True:
        conn, _ = s.accept()
        with conn:
            req = json.loads(conn.makefile().readline())
            try:
                res = {"ok": True, "result": c.call(req["op"], _timeout=req.get("timeout", 90), **req.get("args", {}))}
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            conn.sendall((json.dumps(res) + "\n").encode())


def call(op, _t=90, **args):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(SOCK)
    except OSError:
        sys.exit("no session - start one with: python nano_session.py serve &")
    s.sendall((json.dumps({"op": op, "args": args, "timeout": _t}) + "\n").encode())
    res = json.loads(s.makefile().readline())
    if not res["ok"]:
        sys.exit("ERROR: " + res["error"])
    return res["result"]


def save(img, path):
    open(path, "wb").write(base64.b64decode(img["data"]))
    print(f"{path} {img['width']}x{img['height']}")


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    cmd, r = a[0], a[1:]
    t0 = time.time()
    if cmd == "serve":
        return serve()
    if cmd == "shot":
        save(call("screenshot")["image"], r[0] if r else SHOT)
    elif cmd == "zoom":
        save(call("zoom", region=[int(v) for v in r[:4]])["image"], r[4] if len(r) > 4 else os.path.join(WORK, "zoom.jpg"))
    elif cmd == "click":
        btn = r[2] if len(r) > 2 else "left"
        op = {"left": "left_click", "right": "right_click", "double": "double_click"}[btn]
        call(op, coordinate=[int(r[0]), int(r[1])])
    elif cmd == "type":
        call("type", text=r[0], interval=0.01)
    elif cmd == "key":
        call("key", text=r[0], repeat=int(r[1]) if len(r) > 1 else 1)
    elif cmd == "run":
        t = float(r[1]) if len(r) > 1 else 60
        out = call("run", _t=t + 15, command=r[0], timeout=t)
        sys.stdout.write(out["stdout"])
        if out["stderr"].strip():
            sys.stdout.write("[stderr] " + out["stderr"])
        print(f"[rc={out['rc']}]")
    elif cmd == "op":
        res = call(r[0], **(json.loads(r[1]) if len(r) > 1 else {}))
        if "image" in res:
            save(res["image"], os.path.join(WORK, "op.jpg"))
        else:
            print(json.dumps(res, indent=1)[:4000])
    else:
        sys.exit(f"unknown {cmd}")
    print(f"[{time.time() - t0:.2f}s]", file=sys.stderr)


if __name__ == "__main__":
    main()
