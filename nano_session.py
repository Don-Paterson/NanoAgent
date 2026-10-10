#!/usr/bin/env python3
"""nano_session.py - persistent NanoAgent session for interactive use from a shell
(built for Claude sessions driving A-GUI; see CLAUDE.md).  One background process holds
the encrypted connection, so each command costs ~0.4 s instead of ~4 s.

    python nano_session.py serve &                       # once; holds the connection
    python nano_session.py shot [file]                   # screenshot (1280x720 jpeg)
    python nano_session.py peek [file]                   # cheaper screenshot (quality 40): about half the messages
    python nano_session.py zoom x0 y0 x1 y1 [file]       # full-res crop of a region
    python nano_session.py click x y [left|right|double]
    python nano_session.py type "text"      |  key ctrl+s [repeat]
    python nano_session.py run "PowerShell" [timeout]    # blocks, max 600 s
    python nano_session.py op <op> '<json args>'         # any agent op (windows, scroll, ...)

Long work (up to 2 h) runs on the lab machine and survives a dropped connection:

    python nano_session.py job start script.ps1 [timeout] [name]   # '# relay: timeout=N / watch=GLOB' headers honoured
    python nano_session.py job status [id]                         # a few lines, cheap
    python nano_session.py job tail <id> [lines]                   # end of the output + watched files
    python nano_session.py job kill <id>

Files (each 384 KB chunk is one message; compress or filter on the lab machine first):

    python nano_session.py get <remote> <local> [--tail BYTES]     # chunked
    python nano_session.py put <local> <remote>                    # chunked, overwrites
    python nano_session.py sha <remote>                            # size + SHA-256 to verify a transfer

Cost awareness (Azure Web PubSub Free = 20,000 messages/day, billed in 2 KB units):

    python nano_session.py usage                         # estimate for today from this session's traffic

Every operation is also appended to nano_work/transcript.jsonl (typed text and secrets redacted).

Env: NANO_CONFIG (controller.json, default ./controller.json), NANO_DIR (work dir for the
socket and images, default ./nano_work).  Coordinates are screenshot pixels.
"""
import base64, json, os, socket, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CFG = os.environ.get("NANO_CONFIG", "controller.json")
WORK = os.path.abspath(os.environ.get("NANO_DIR", "nano_work"))
os.makedirs(WORK, exist_ok=True)
SOCK = os.path.join(WORK, "session.sock")
SHOT = os.path.join(WORK, "now.jpg")
USAGE = os.path.join(WORK, "usage.json")
TRANSCRIPT = os.path.join(WORK, "transcript.jsonl")
PEEK_QUALITY = 40


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _limit():
    try:
        return int(json.load(open(CFG, encoding="utf-8-sig")).get("daily_message_limit", 20000))
    except (OSError, ValueError):
        return 20000


def _load_usage():
    try:
        u = json.load(open(USAGE))
        if u.get("date") == _today():
            return u
    except (OSError, ValueError):
        pass
    return {"date": _today(), "units": 0, "msgs": 0}


def _summary(res):
    out = {}
    for k, v in (res or {}).items():
        if k == "image":
            out[k] = f"{v.get('width')}x{v.get('height')} {len(v.get('data', ''))} b64 chars"
        elif k == "data":
            out[k] = f"<{len(v)} b64 chars>"
        elif isinstance(v, str):
            out[k] = v if len(v) <= 1500 else v[:1500] + f"...<{len(v)} chars>"
        else:
            out[k] = v
    return out


def serve():
    from nano_client import NanoClient
    from nano_safety import redact_args
    c = NanoClient.from_config(CFG, timeout=60)
    c.wait_for_agent(30)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    s = socket.socket(socket.AF_UNIX); s.bind(SOCK); s.listen(4)
    base = c.usage()
    seen = {"units": base["units"], "msgs": base["msgs_out"] + base["msgs_in"]}
    print("session ready", flush=True)
    while True:
        conn, _ = s.accept()
        with conn:
            req = json.loads(conn.makefile().readline())
            op = req["op"]
            if op == "__usage__":
                u = _load_usage()
                conn.sendall((json.dumps({"ok": True, "result": dict(u, limit=_limit())}) + "\n").encode())
                continue
            try:
                if op == "__get__":
                    a = req.get("args", {})
                    n = c.get_file(a["remote"], a["local"], tail=a.get("tail"))
                    r = {"bytes": n, "local": a["local"]}
                elif op == "__put__":
                    a = req.get("args", {})
                    r = {"path": c.put_file(a["local"], a["remote"], overwrite=True)}
                else:
                    r = c.call(op, _timeout=req.get("timeout", 90), **req.get("args", {}))
                res = {"ok": True, "result": r}
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            now = c.usage()
            cur = {"units": now["units"], "msgs": now["msgs_out"] + now["msgs_in"]}
            u = _load_usage()
            u["units"] += cur["units"] - seen["units"]
            u["msgs"] += cur["msgs"] - seen["msgs"]
            seen = cur
            json.dump(u, open(USAGE, "w"))
            res["usage"] = {"units_today": u["units"], "limit": _limit()}
            try:
                with open(TRANSCRIPT, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "op": op,
                                        "args": redact_args(op, req.get("args", {})), "ok": res["ok"],
                                        "result": _summary(res.get("result")) if res["ok"] else res["error"],
                                        "units_today": u["units"]}) + "\n")
            except OSError:
                pass
            conn.sendall((json.dumps(res) + "\n").encode())


def call(op, _t=90, **args):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(SOCK)
    except OSError:
        sys.exit("no session - start one with: python nano_session.py serve &")
    s.sendall((json.dumps({"op": op, "args": args, "timeout": _t}) + "\n").encode())
    res = json.loads(s.makefile().readline())
    u = res.get("usage")
    if u and u["units_today"] > 0.7 * u["limit"]:
        pct = 100 * u["units_today"] / u["limit"]
        print(f"[usage] about {u['units_today']} of {u['limit']} daily messages used ({pct:.0f}%): "
              f"use peek/zoom/run instead of screenshots", file=sys.stderr)
    if not res["ok"]:
        sys.exit("ERROR: " + res["error"])
    return res["result"]


def save(img, path):
    open(path, "wb").write(base64.b64decode(img["data"]))
    print(f"{path} {img['width']}x{img['height']}")


def _job_line(j):
    el = f" {j['elapsed']}s" if j.get("elapsed") is not None else ""
    return f"{j['id']}  {j['status']}{el}  rc={j['rc']}  timeout={j['timeout']}s  output={j['output_bytes']}B"


def _print_tail(r):
    print(_job_line(r["job"]))
    o = r["output"]
    print(f"--- output ({o.get('size')} bytes{', truncated' if o.get('truncated') else ''}) ---")
    sys.stdout.write(o.get("text", o.get("error", "")) + "\n")
    for w in r.get("watched", []):
        print(f"--- watched: {w['path']} ({w.get('size')} bytes) ---")
        sys.stdout.write(w.get("text", w.get("error", "")) + "\n")


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
    elif cmd == "peek":
        save(call("screenshot", quality=PEEK_QUALITY)["image"], r[0] if r else SHOT)
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
    elif cmd == "job":
        sub = r[0] if r else ""
        if sub == "start":
            text = open(r[1], encoding="utf-8-sig").read()
            args = {"script": text, "shell": "pwsh", "name": os.path.splitext(os.path.basename(r[1]))[0]}
            if len(r) > 2 and r[2].isdigit():
                args["timeout"] = int(r[2])
            if len(r) > 3:
                args["name"] = r[3]
            j = call("job_start", **args)
            print(_job_line(j))
        elif sub == "status":
            res = call("job_status", **({"id": r[1]} if len(r) > 1 else {}))
            for j in res["jobs"]:
                print(_job_line(j))
            if not res["jobs"]:
                print("(no jobs)")
        elif sub == "tail":
            _print_tail(call("job_tail", id=r[1], lines=int(r[2]) if len(r) > 2 else 40))
        elif sub == "kill":
            print(_job_line(call("job_kill", id=r[1])["job"]))
        else:
            sys.exit("job start <script> [timeout] [name] | status [id] | tail <id> [lines] | kill <id>")
    elif cmd == "get":
        tail = int(r[r.index("--tail") + 1]) if "--tail" in r else None
        res = call("__get__", _t=600, remote=r[0], local=r[1], tail=tail)
        print(f"{res['local']} {res['bytes']} bytes")
    elif cmd == "put":
        res = call("__put__", _t=600, local=r[0], remote=r[1])
        print(res["path"])
    elif cmd == "sha":
        res = call("file_sha256", path=r[0])
        print(f"{res['sha256']}  {res['size']} bytes  {res['path']}")
    elif cmd == "usage":
        u = call("__usage__")
        print(f"{u['date']} UTC: about {u['units']} of {u['limit']} daily messages ({100 * u['units'] / u['limit']:.1f}%), "
              f"{u['msgs']} messages sent/received by this session. Estimate only: NanoAzure.ps1 option U is the real figure.")
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
