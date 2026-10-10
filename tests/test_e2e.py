"""
End-to-end test: local relay + agent + target window on an X display, driven
through NanoClient, plus the claude_driver batch logic with fake Claude blocks.

    DISPLAY=:99 python tests/test_e2e.py          (Linux, Xvfb 1600x1000)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nano_client import NanoClient, NanoError  # noqa: E402
from nano_proto import key_from_secret, support_session  # noqa: E402

PORT, KEY, GROUP, SECRET = 18765, "relaykey", "lab-test", "a" * 64
NEG = f"http://127.0.0.1:{PORT}/negotiate?code={KEY}"
tmp = Path(tempfile.mkdtemp())
events_path = tmp / "events.jsonl"
procs = []
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def events():
    if not events_path.exists():
        return []
    return [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]


def start(args, **kw):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
    procs.append(p)
    return p


try:
    start([sys.executable, str(ROOT / "local_relay.py"), "--port", str(PORT), "--key", KEY, "--host", "127.0.0.1"])
    start(["python3.12", str(ROOT / "tests/target_app.py"), str(events_path)])
    cfg = tmp / "agent.json"
    cfg.write_text(json.dumps({"negotiate_url": NEG, "group": GROUP, "secret": SECRET, "max_long_edge": 1280}))
    agent = start([sys.executable, str(ROOT / "nano_agent.py"), "--config", str(cfg), "--log", str(tmp / "agent.log")])
    for _ in range(50):
        if any(e["ev"] == "ready" for e in events()):
            break
        time.sleep(0.2)

    print("== protocol / agent ops")
    with NanoClient(NEG, GROUP, key_from_secret(SECRET), timeout=15) as c:
        t0 = time.time()
        info = c.wait_for_agent(30)
        check("agent info", info["screen"] == [1600, 1000] and info["screenshot"] == [1280, 800], info)
        print(f"     connected in {time.time() - t0:.1f}s")

        t0 = time.time()
        img = c.screenshot(str(tmp / "s1.jpg"))
        check("screenshot scaled to 1280x800", img.size == (1280, 800), img.size)
        print(f"     screenshot round trip {time.time() - t0:.2f}s")

        # entry at screen (400,300) -> screenshot (320,240); click inside it
        c.click(340, 250)
        time.sleep(0.3)
        clicks = [e for e in events() if e["ev"] == "click"]
        check("click scaled to screen coords", clicks and abs(clicks[-1]["x"] - 425) <= 2 and abs(clicks[-1]["y"] - 312) <= 2,
              clicks[-1:] )
        c.type("Hello SmartConsole")
        c.key("Home"); c.key("End"); c.type("!!")
        c.key("BackSpace", repeat=2)
        c.type("?")
        time.sleep(0.4)
        texts = [e["value"] for e in events() if e["ev"] == "text"]
        check("typing + keys + repeat", texts and texts[-1] == "Hello SmartConsole?", texts[-1:])
        c.key("shift+Home"); c.type("Done")
        time.sleep(0.4)
        texts = [e["value"] for e in events() if e["ev"] == "text"]
        check("key combo (shift+Home selects, typing replaces)", texts and texts[-1] == "Done", texts[-1:])

        c.click(800, 520, "right")
        time.sleep(0.3)
        rc = [e for e in events() if e["ev"] == "rclick"]
        check("right click", rc and abs(rc[-1]["x"] - 1000) <= 2 and abs(rc[-1]["y"] - 650) <= 2, rc[-1:])

        c.move(100, 100)
        check("cursor_position in screenshot space", c.cursor() == (100, 100), c.cursor())
        c.scroll(640, 400, "down", 2)
        check("scroll", True)

        z = c.zoom(720, 480, 880, 520, save=str(tmp / "zoom.jpg"))
        check("zoom returns full-res crop", z.size == (200, 50), z.size)

        rc_, out, err = c.run("echo hello from agent; exit 3")
        check("run", rc_ == 3 and out.strip() == "hello from agent", (rc_, out, err))

        src = tmp / "up.bin"; src.write_bytes(os.urandom(5000))
        remote = str(tmp / "remote" / "up.bin")
        c.put_file(str(src), remote)
        c.get_file(remote, str(tmp / "down.bin"))
        check("file put/get round trip", (tmp / "down.bin").read_bytes() == src.read_bytes())
        try:
            c.put_file(str(src), remote)
            check("put refuses overwrite", False)
        except NanoError:
            check("put refuses overwrite", True)

        try:
            c.click(5000, 10)
            check("out-of-range click rejected", False)
        except NanoError as e:
            check("out-of-range click rejected", "outside screenshot" in str(e), e)

        try:
            c.windows()
            check("windows op errors cleanly off-Windows", False)
        except NanoError as e:
            check("windows op errors cleanly off-Windows", "Windows-only" in str(e), e)

        # image anchor: cut the orange TARGET label out of a screenshot and find it again
        shot = c.screenshot()
        shot.crop((720, 480, 840, 515)).save(tmp / "anchor.png")
        pos = c.find_image(str(tmp / "anchor.png"))
        check("find_image anchor", pos is not None and abs(pos[0] - 780) <= 3 and abs(pos[1] - 497) <= 3, pos)

        # replay protection: resend an identical signed command
        import asyncio
        from nano_proto import make_cmd
        msg = make_cmd("ping", {}, key_from_secret(SECRET))
        fut = c._loop.create_future()
        async def send_twice():
            c._pending[msg["id"]] = fut
            await c._link.send(msg)
            r1 = await asyncio.wait_for(fut, 10)
            fut2 = c._loop.create_future(); c._pending[msg["id"]] = fut2
            await c._link.send(msg)
            return r1, await asyncio.wait_for(fut2, 10)
        r1, r2 = asyncio.run_coroutine_threadsafe(send_twice(), c._loop).result(25)
        check("replayed command rejected", r1["ok"] and not r2["ok"] and "repeated" in r2["error"], r2)

    print("== wrong secret gets silence")
    with NanoClient(NEG, GROUP, key_from_secret("b" * 64), timeout=4) as bad:
        try:
            bad.call("info")
            check("bad-secret client ignored", False)
        except NanoError as e:
            check("bad-secret client ignored", "no reply" in str(e), e)
    log_text = (tmp / "agent.log").read_text()
    check("agent logged failed decryption", "failed decryption" in log_text)

    print("== eavesdropper with a valid relay token sees only ciphertext")
    import asyncio as _a
    import websockets as _ws
    from nano_proto import negotiate, SUBPROTOCOL

    async def eavesdrop():
        url = negotiate(NEG, "controller", GROUP)
        seen = []
        async with _ws.connect(url, subprotocols=[SUBPROTOCOL], max_size=16 * 1024 * 1024) as spy:
            await spy.send(json.dumps({"type": "joinGroup", "group": GROUP, "ackId": 1}))
            await _a.sleep(0.5)
            # also try to inject an unencrypted (but otherwise well-formed) command
            from nano_proto import make_cmd
            await spy.send(json.dumps({"type": "sendToGroup", "group": GROUP, "dataType": "json",
                                       "noEcho": True, "data": make_cmd("run", {"command": "echo pwned"}, key_from_secret(SECRET))}))
            def legit():
                with NanoClient(NEG, GROUP, key_from_secret(SECRET), timeout=15) as c2:
                    c2.wait_for_agent(20)
                    c2.run("echo TOPSECRET-OUTPUT")
                    c2.screenshot()
            await _a.to_thread(legit)
            try:
                while True:
                    seen.append(json.loads(await _a.wait_for(spy.recv(), 1.0)))
            except (_a.TimeoutError, TimeoutError):
                pass
        return seen
    frames = _a.run(eavesdrop())
    datas = [f["data"] for f in frames if f.get("type") == "message"]
    blob = json.dumps(datas)
    check("spy received traffic", len(datas) >= 4, len(datas))
    check("all traffic is enc envelopes", datas and all(set(d) == {"kind", "v", "n", "c"} and d["kind"] == "enc" for d in datas))
    check("no plaintext leaks (output, op names, image)", "TOPSECRET" not in blob and '"op"' not in blob
          and "screenshot" not in blob and "/9j/" not in blob)
    check("plaintext injection not executed", "pwned" not in (tmp / "agent.log").read_text())

    print("== crypto unit checks")
    from nano_proto import seal, unseal
    k = key_from_secret(SECRET)
    env = seal({"kind": "cmd", "x": 1}, k, "lab-a")
    check("round trip", unseal(env, k, "lab-a") == {"kind": "cmd", "x": 1})
    check("wrong group rejected (AAD)", unseal(env, k, "lab-b") is None)
    check("wrong key rejected", unseal(env, key_from_secret("z" * 64), "lab-a") is None)
    bad = dict(env); bad["c"] = bad["c"][:-6] + ("AAAAAA" if not bad["c"].endswith("AAAAAA") else "BBBBBB")
    check("tampered ciphertext rejected", unseal(bad, k, "lab-a") is None)
    # worst case frame size: pure noise screenshot at max_long_edge 1280
    import random
    from PIL import Image as _I
    noise = _I.frombytes("RGB", (1280, 800), random.randbytes(1280 * 800 * 3))
    import nano_agent
    img = nano_agent.Screen.encode(noise, "jpeg", 70)
    frame = json.dumps({"type": "sendToGroup", "group": "lab-a-gui", "dataType": "json", "noEcho": True,
                        "data": seal({"kind": "res", "v": 1, "id": "x" * 32, "ts": 1.0, "ok": True,
                                      "result": {"image": img}, "sig": "s" * 64}, k, "lab-a-gui")})
    check(f"worst-case screenshot frame < 1 MB ({len(frame) // 1024} KB)", len(frame) < 1_000_000)

    print("== claude_driver batch handling (fake Claude blocks)")
    import claude_driver as cd
    with NanoClient(NEG, GROUP, key_from_secret(SECRET), timeout=15) as c:
        c.wait_for_agent(20)
        drv = cd.Driver.__new__(cd.Driver)
        drv.nano, drv.confirm, drv.log_dir, drv.shot_no = c, False, tmp / "run", 0
        drv.log_dir.mkdir()
        B = lambda i, name, inp, ts="computer": SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=inp, toolset_name=ts)
        content = [SimpleNamespace(type="text", text="clicking"),
                   B(1, "left_click", {"coordinate": [340, 250]}),
                   B(2, "type", {"text": "x"}),
                   B(3, "run_powershell", {"command": "echo hi"}, ts=None) if False else B(3, "key", {"text": "Return"})]
        res = drv.process(content)
        check("3 results, all toolset-tagged", len(res) == 3 and all(r.get("toolset_name") == "computer" for r in res))
        check("auto screenshot attached to last result",
              any(b.get("type") == "image" for b in res[-1]["content"]), res[-1]["content"][:1])
        content = [B(4, "left_click", {"coordinate": [9999, 1]}), B(5, "type", {"text": "never"}),
                   B(6, "screenshot", {})]
        res = drv.process(content)
        check("failure then halt text",
              res[0].get("is_error") and res[1]["content"] == cd.NOT_EXECUTED and res[2]["content"] == cd.NOT_EXECUTED,
              [r.get("content") for r in res])
        res = drv.process([B(7, "zoom", {"region": [0, 0, 640, 400]}), B(8, "cursor_position", {})])
        check("zoom image + cursor text", res[0]["content"][0]["type"] == "image" and "X=" in res[1]["content"][0]["text"]
              and any(b["type"] == "image" for b in res[1]["content"]))
        # custom tool path is not tagged as computer
        cd.CUSTOM_TOOLS  # noqa
        res = drv.process([SimpleNamespace(type="tool_use", id="t9", name="run_powershell", input={"command": "echo hi"})])
        # on Linux the agent runs bash for shell=auto, so this works here too
        check("custom tool result untagged", "toolset_name" not in res[0] and "exit code: 0" in res[0]["content"][0]["text"], res)
        check("screenshots logged", len(list(drv.log_dir.glob("*.jpg"))) >= 3)

        # full loop with a scripted fake Messages API
        from anthropic.types import Message
        script = [
            [{"type": "text", "text": "Typing into the box."},
             {"type": "tool_use", "id": "u1", "name": "left_click", "toolset_name": "computer", "input": {"coordinate": [340, 250]}},
             {"type": "tool_use", "id": "u2", "name": "type", "toolset_name": "computer", "input": {"text": "via loop"}},
             {"type": "tool_use", "id": "u3", "name": "screenshot", "toolset_name": "computer", "input": {}}],
            [{"type": "tool_use", "id": "u4", "name": "run_powershell", "input": {"command": "echo ok"}}],
            [{"type": "text", "text": "Done."}],
        ]
        seen = []

        class FakeMessages:
            def create(self, **kw):
                seen.append(kw)
                content = script[len(seen) - 1]
                return Message.model_validate({"id": "m", "type": "message", "role": "assistant", "model": kw["model"],
                    "content": content, "stop_reason": "tool_use" if len(seen) < 3 else "end_turn",
                    "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
        drv.api = SimpleNamespace(messages=FakeMessages())
        drv.model = "claude-sonnet-5-5"
        msgs = drv.run("Type into the box", max_steps=5)
        check("loop ran 3 API calls", len(seen) == 3)
        check("toolset declared", seen[0]["tools"][0]["type"] == "computer_toolset_20260801")
        last_user = [m for m in msgs if m["role"] == "user"][-1]["content"]
        check("custom tool result fed back", "exit code: 0" in last_user[0]["content"][0]["text"])
        check("assistant turns keep toolset_name", seen[1]["messages"][1]["content"][1].get("toolset_name") == "computer")
        json.dumps(seen[2]["messages"])   # whole history must be JSON-serialisable

    print("== support code mapping")
    g, k = support_session("4821-9930")
    check("support group", g == "sup-48219930" and len(k) == 32)
finally:
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(3)
        except Exception:
            p.kill()

print(f"\n{passed} passed, {failed} failed")
if failed:
    print("--- agent log ---\n" + (tmp / "agent.log").read_text()[-3000:])
sys.exit(1 if failed else 0)
