#!/usr/bin/env python3
"""
nano_cli.py - command line / demo runner for NanoAgent.

    python nano_cli.py info
    python nano_cli.py shot -o now.jpg
    python nano_cli.py click 120 85            [--button right|double] [--mods ctrl]
    python nano_cli.py type "Hello"            ;  python nano_cli.py key ctrl+s
    python nano_cli.py run "Get-Process SmartConsole"
    python nano_cli.py windows                 ;  python nano_cli.py focus SmartConsole
    python nano_cli.py place SmartConsole 0 0 1280 760
    python nano_cli.py crop now.jpg 400 300 520 330 -o anchors/publish.png   (local, no agent)
    python nano_cli.py play demos/publish.json
    python nano_cli.py repl
    python nano_cli.py --support 4821-9930 shot -o user.jpg

Config: controller.json (negotiate_url, group, secret) or NANO_* env vars.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent


def client(a):
    from nano_client import NanoClient
    return NanoClient.from_config(a.config, support_code=a.support, timeout=a.timeout)


# --------------------------------------------------------------------------- steps
def do_step(c, step: dict, base: Path):
    """One demo step.  Keys:
       op + args          any agent op, e.g. {"op":"left_click","coordinate":[120,85]}
       click:[x,y]        shorthand;  "button":"right"/"double", "mods":"ctrl"
       click_image:file   wait for anchor image then click it ("timeout", "threshold")
       wait_image:file    wait for anchor image
       type, key, run, focus, place:[title,x,y,w,h], shot:file, sleep:seconds, say:text
    """
    if "say" in step:
        print(f"  # {step['say']}")
    if "focus" in step:
        c.focus_window(step["focus"])
    if "place" in step:
        t, x, y, w, h = step["place"]
        c.move_window(t, x, y, w, h)
    if "wait_image" in step:
        c.wait_image(str(base / step["wait_image"]), step.get("timeout", 15), step.get("threshold", 0.9))
    if "click_image" in step:
        c.click_image(str(base / step["click_image"]), step.get("timeout", 15),
                      step.get("threshold", 0.9), step.get("button", "left"))
    if "click" in step:
        x, y = step["click"]
        c.click(x, y, step.get("button", "left"), step.get("mods"))
    if "type" in step:
        c.type(step["type"], step.get("interval", 0.04))
    if "key" in step:
        c.key(step["key"], step.get("repeat", 1))
    if "run" in step:
        rc, out, err = c.run(step["run"], step.get("timeout", 60))
        print(out.rstrip())
        if rc and not step.get("ignore_rc"):
            raise SystemExit(f"run failed rc={rc}: {err.strip()}")
    if "op" in step:
        args = {k: v for k, v in step.items() if k not in ("op", "say", "sleep")}
        r = c.call(step["op"], **args)
        if r and "image" not in r:
            print(json.dumps(r, indent=1)[:2000])
    if "shot" in step:
        c.screenshot(str(base / step["shot"]))
    if "sleep" in step:
        time.sleep(float(step["sleep"]))


# --------------------------------------------------------------------------- commands
def cmd_info(c, a):
    print(json.dumps(c.wait_for_agent(a.timeout), indent=2))


def cmd_shot(c, a):
    img = c.zoom(*a.zoom, save=a.out) if a.zoom else c.screenshot(a.out)
    print(f"saved {a.out} ({img.width}x{img.height})")


def cmd_click(c, a):
    c.click(a.x, a.y, a.button, a.mods)


def cmd_move(c, a):
    c.move(a.x, a.y)


def cmd_drag(c, a):
    c.drag(a.x1, a.y1, a.x2, a.y2)


def cmd_scroll(c, a):
    c.scroll(a.x, a.y, a.direction, a.amount)


def cmd_type(c, a):
    c.type(a.text, a.interval)


def cmd_key(c, a):
    c.key(a.combo, a.repeat)


def cmd_where(c, a):
    print("%d %d" % c.cursor())


def cmd_run(c, a):
    rc, out, err = c.run(a.command, a.run_timeout, a.shell)
    sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    raise SystemExit(rc)


def cmd_windows(c, a):
    for w in c.windows():
        l, t, r, b = w["rect_shot"]
        print(f"{l:5d},{t:5d} {r - l:5d}x{b - t:<5d} {'(min) ' if w['minimized'] else ''}{w['title']}")


def cmd_focus(c, a):
    print(c.focus_window(a.title))


def cmd_place(c, a):
    print(c.move_window(a.title, a.x, a.y, a.w, a.h))


def cmd_get(c, a):
    print(f"{c.get_file(a.remote, a.local)} bytes -> {a.local}")


def cmd_put(c, a):
    print(c.put_file(a.local, a.remote, a.overwrite))


def cmd_find(c, a):
    pos = c.find_image(a.template, a.threshold)
    print(f"{pos[0]} {pos[1]}" if pos else "not found")
    raise SystemExit(0 if pos else 1)


def cmd_play(c, a):
    path = Path(a.file)
    steps = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(steps, dict):
        steps = steps.get("steps", [])
    c.wait_for_agent(30)
    for i, step in enumerate(steps, 1):
        print(f"[{i}/{len(steps)}] {json.dumps(step)[:110]}")
        if a.step:
            input("    enter to run > ")
        do_step(c, step, path.parent)
    print("done")


def cmd_repl(c, a):
    print("NanoAgent REPL - e.g. 'click 100 200', 'type hello', 'key ctrl+s', 'shot now.jpg',\n"
          "'run Get-Date', 'focus SmartConsole', 'windows', 'where', or raw JSON step. 'quit' to exit.")
    c.wait_for_agent(30)
    while True:
        try:
            line = input("nano> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("quit", "exit"):
            break
        try:
            if line.startswith("{"):
                do_step(c, json.loads(line), Path.cwd())
                continue
            w = shlex.split(line, posix=True)
            cmd, rest = w[0], w[1:]
            if cmd == "click":
                c.click(int(rest[0]), int(rest[1]), rest[2] if len(rest) > 2 else "left")
            elif cmd == "type":
                c.type(" ".join(rest))
            elif cmd == "key":
                c.key(rest[0], int(rest[1]) if len(rest) > 1 else 1)
            elif cmd == "shot":
                c.screenshot(rest[0] if rest else "shot.jpg")
                print("saved")
            elif cmd == "run":
                rc, out, err = c.run(line[4:])
                print(out + err + f"[rc={rc}]")
            elif cmd == "focus":
                print(c.focus_window(" ".join(rest)))
            elif cmd == "windows":
                cmd_windows(c, a)
            elif cmd == "where":
                print("%d %d" % c.cursor())
            elif cmd == "scroll":
                c.scroll(int(rest[0]), int(rest[1]), rest[2], int(rest[3]) if len(rest) > 3 else 3)
            else:
                print(json.dumps(c.call(cmd, **json.loads(" ".join(rest) or "{}")), indent=1)[:3000])
        except Exception as e:
            print(f"error: {e}")


def cmd_crop(a):
    img = Image.open(a.image)
    img.crop((a.x0, a.y0, a.x1, a.y1)).save(a.out)
    print(f"saved {a.out}")


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="NanoAgent controller CLI")
    ap.add_argument("--config", default=str(HERE / "controller.json"))
    ap.add_argument("--support", metavar="CODE", help="connect to a support session code instead")
    ap.add_argument("--timeout", type=float, default=30)
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("info").set_defaults(fn=cmd_info)
    p = sp.add_parser("shot"); p.add_argument("-o", "--out", default="shot.jpg")
    p.add_argument("--zoom", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1")); p.set_defaults(fn=cmd_shot)
    p = sp.add_parser("click"); p.add_argument("x", type=int); p.add_argument("y", type=int)
    p.add_argument("--button", default="left", choices=["left", "right", "middle", "double", "triple"])
    p.add_argument("--mods"); p.set_defaults(fn=cmd_click)
    p = sp.add_parser("move"); p.add_argument("x", type=int); p.add_argument("y", type=int); p.set_defaults(fn=cmd_move)
    p = sp.add_parser("drag")
    for n in ("x1", "y1", "x2", "y2"):
        p.add_argument(n, type=int)
    p.set_defaults(fn=cmd_drag)
    p = sp.add_parser("scroll"); p.add_argument("x", type=int); p.add_argument("y", type=int)
    p.add_argument("direction", choices=["up", "down", "left", "right"]); p.add_argument("amount", type=int, nargs="?", default=3)
    p.set_defaults(fn=cmd_scroll)
    p = sp.add_parser("type"); p.add_argument("text"); p.add_argument("--interval", type=float, default=0.02); p.set_defaults(fn=cmd_type)
    p = sp.add_parser("key"); p.add_argument("combo"); p.add_argument("--repeat", type=int, default=1); p.set_defaults(fn=cmd_key)
    sp.add_parser("where").set_defaults(fn=cmd_where)
    p = sp.add_parser("run"); p.add_argument("command"); p.add_argument("--run-timeout", type=float, default=60)
    p.add_argument("--shell", default="auto", choices=["auto", "powershell", "cmd", "bash"]); p.set_defaults(fn=cmd_run)
    sp.add_parser("windows").set_defaults(fn=cmd_windows)
    p = sp.add_parser("focus"); p.add_argument("title"); p.set_defaults(fn=cmd_focus)
    p = sp.add_parser("place"); p.add_argument("title")
    for n in ("x", "y", "w", "h"):
        p.add_argument(n, type=int)
    p.set_defaults(fn=cmd_place)
    p = sp.add_parser("get"); p.add_argument("remote"); p.add_argument("local"); p.set_defaults(fn=cmd_get)
    p = sp.add_parser("put"); p.add_argument("local"); p.add_argument("remote")
    p.add_argument("--overwrite", action="store_true"); p.set_defaults(fn=cmd_put)
    p = sp.add_parser("find"); p.add_argument("template"); p.add_argument("--threshold", type=float, default=0.9)
    p.set_defaults(fn=cmd_find)
    p = sp.add_parser("play"); p.add_argument("file"); p.add_argument("--step", action="store_true",
                                                                       help="pause before each step")
    p.set_defaults(fn=cmd_play)
    sp.add_parser("repl").set_defaults(fn=cmd_repl)
    p = sp.add_parser("crop", help="cut an anchor image from a saved screenshot (local)")
    p.add_argument("image")
    for n in ("x0", "y0", "x1", "y1"):
        p.add_argument(n, type=int)
    p.add_argument("-o", "--out", required=True); p.set_defaults(local=cmd_crop)

    a = ap.parse_args(argv)
    if getattr(a, "local", None):
        return a.local(a)
    c = client(a)
    try:
        a.fn(c, a)
    finally:
        c.close()


if __name__ == "__main__":
    main()
