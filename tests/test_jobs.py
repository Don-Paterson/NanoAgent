"""
Tests for NanoAgent 1.2: jobs, chunked files, pwsh, redaction, TLS guard, usage counters.
No screen is needed for most of it; the agent runs against local_relay.py with pyautogui stubbed
(only mss needs a display, so run under Xvfb):

    Xvfb :99 -screen 0 1600x1000x24 &  DISPLAY=:99 python tests/test_jobs.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def wait_for(fn, timeout=15, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


# =========================================================================== unit: header parsing, redaction, guard
from nano_jobs import JobManager, parse_header, pid_alive, find_pwsh  # noqa: E402
from nano_safety import TlsGuardError, make_guard, redact_args, redact_text  # noqa: E402

print("== header comments")
h = parse_header("# relay: timeout=5400 progress=60\n# relay: watch=C:\\Logs\\*.log\n# relay: watch=C:\\X\\a.txt\nGet-Date\n")
check("timeout/progress/watch parsed", h == {"timeout": 5400, "progress": 60, "watch": ["C:\\Logs\\*.log", "C:\\X\\a.txt"]}, h)

print("== redaction")
check("typed text never logged", redact_args("type", {"text": "Chkp!234", "interval": 0.01}) == {"text": "<8 chars>", "interval": 0.01})
for cmd in ("$password = 'Chkp!234'; do-it", "plink -pw Chkp!234 admin@10.1.1.101 ls", "x -Password 'Chkp!234' y",
            "$s = ConvertTo-SecureString 'Chkp!234' -AsPlainText -Force", "curl --token=abc123def"):
    r = redact_text(cmd)
    check(f"redacts: {cmd[:34]}", "Chkp!234" not in r and "abc123def" not in r and "<redacted>" in r, r)
check("ordinary command unchanged", redact_text("Get-Service CP* | ft -auto") == "Get-Service CP* | ft -auto")
check("file_put data summarised", redact_args("file_put", {"path": "C:\\a", "data": "A" * 5000})["data"] == "<5000 chars>")

print("== TLS guard")
res = {}
def fake(host, port):
    return res[host]
g = make_guard({"tls_guard": "enforce"}, checker=fake)
res["good.example"] = {"ok": True, "org": "Microsoft Corporation", "issuer": "x", "error": None}
g("https://good.example/api")
check("trusted issuer accepted", True)
res["bad.example"] = {"ok": True, "org": "Fortinet", "issuer": "CN=FG6H0E", "error": None}
try:
    g("https://bad.example/api"); check("inspecting issuer blocked", False)
except TlsGuardError as e:
    check("inspecting issuer blocked", "Fortinet" in str(e) or "FG6H0E" in str(e), e)
res["invalid.example"] = {"ok": False, "org": "", "issuer": "", "error": "certificate not valid: self-signed"}
try:
    g("wss://invalid.example/x"); check("invalid certificate blocked", False)
except TlsGuardError:
    check("invalid certificate blocked", True)
res["down.example"] = {"ok": None, "org": "", "issuer": "", "error": "OSError: unreachable"}
g("https://down.example/")
check("unreachable host is not a block", True)
g("ws://127.0.0.1:1234/x")
check("plain ws:// (local relay) skipped", True)
gw = make_guard({"tls_guard": "warn"}, checker=fake)
gw("https://bad.example/api")
check("warn mode continues", True)
check("off mode has no guard", make_guard({"tls_guard": "off"}) is None)
msgs = []
g2 = make_guard({"tls_guard": "enforce"}, status_cb=msgs.append, checker=fake)
try:
    g2("https://bad.example/")
except TlsGuardError:
    pass
check("banner message on block", msgs and "blocked" in msgs[0], msgs)

# =========================================================================== unit: job manager
print("== job manager")
tmp = Path(tempfile.mkdtemp())
jm = JobManager(tmp)
SH = "bash"        # tests run on Linux; the same code paths serve pwsh/powershell on Windows

j = jm.start(script="echo hello; echo oops >&2; exit 0", shell=SH, name="quick")
r = wait_for(lambda: (jm.status(j["id"])["jobs"][0]["status"] != "running") and jm.status(j["id"])["jobs"][0])
check("job finishes done rc=0", r and r["status"] == "done" and r["rc"] == 0, r)
t = jm.tail(j["id"])
check("output holds stdout and stderr", "hello" in t["output"]["text"] and "oops" in t["output"]["text"], t)

j = jm.start(script="echo bad; exit 7", shell=SH, name="fail")
r = wait_for(lambda: jm.status(j["id"])["jobs"][0]["status"] != "running" and jm.status(j["id"])["jobs"][0])
check("non-zero exit = failed with rc", r and r["status"] == "failed" and r["rc"] == 7, r)

# timeout kills the whole tree (child sleeper must die too)
marker = tmp / "child.pid"
script = f"# relay: timeout=5\n(sleep 300 & echo $! > {marker}; wait)\n"
j = jm.start(script=script, shell=SH, name="hang")
check("header timeout used", jm.status(j["id"])["jobs"][0]["timeout"] == 5, jm.status(j["id"]))
wait_for(lambda: marker.exists() and marker.read_text().strip(), 5)
child = int(marker.read_text().strip())
check("child process started", pid_alive(child))
r = wait_for(lambda: jm.status(j["id"])["jobs"][0]["status"] != "running" and jm.status(j["id"])["jobs"][0], 20)
check("hang -> timeout", r and r["status"] == "timeout", r)
time.sleep(0.5)
check("timeout killed the child too (tree kill)", not pid_alive(child))

# kill
j = jm.start(script="sleep 300", shell=SH, name="killme")
time.sleep(0.5)
k = jm.kill(j["id"])
check("job_kill stops a running job", k["job"]["status"] == "killed", k)

# watched files + tail limits
wf = tmp / "watched.log"
wf.write_text("\n".join(f"log line {i}" for i in range(500)))
j = jm.start(script=f"# relay: watch={wf}\nfor i in $(seq 1 200); do echo out $i; done", shell=SH, name="watch")
wait_for(lambda: jm.status(j["id"])["jobs"][0]["status"] != "running", 10)
t = jm.tail(j["id"], lines=5)
check("tail returns only the last lines", t["output"]["text"].strip().splitlines()[-1] == "out 200"
      and len(t["output"]["text"].splitlines()) <= 5, t["output"]["text"][-60:])
check("watched file tailed", t.get("watched") and t["watched"][0]["text"].strip().splitlines()[-1] == "log line 499", t.get("watched"))
check("tail reply is small", len(json.dumps(t)) < 6000, len(json.dumps(t)))

# survives the manager (agent restart): re-attach, and mark dead ones abandoned
j = jm.start(script="sleep 3; echo after-restart", shell=SH, name="survivor")
jm2 = JobManager(tmp)
jm2.recover()
r = wait_for(lambda: jm2.status(j["id"])["jobs"][0]["status"] != "running" and jm2.status(j["id"])["jobs"][0], 15)
check("job outlives a restarted agent and is re-attached", r and r["status"] in ("ended", "done"), r)
d = jm.root / "20200101-000000-ghost-abcd"
d.mkdir()
(d / "output.txt").write_text("")
(d / "meta.json").write_text(json.dumps({"id": d.name, "status": "running", "pid": 999999, "started_epoch": time.time(),
                                         "timeout": 60, "output": str(d / "output.txt")}))
jm2.recover()
check("dead running job marked abandoned", jm2.status(d.name)["jobs"][0]["status"] == "abandoned", jm2.status(d.name))

try:
    jm.status("../etc")
    check("job id path traversal refused", False)
except (ValueError, FileNotFoundError):
    check("job id path traversal refused", True)

# =========================================================================== usage counters
from nano_proto import PubSubLink  # noqa: E402
check("1 byte and 2048 bytes = 1 unit, 2049 = 2", [PubSubLink._units(1), PubSubLink._units(2048), PubSubLink._units(2049)] == [1, 1, 2])

# =========================================================================== e2e through local relay (headless agent)
print("== agent ops through the local relay")
PORT, KEY, GROUP, SECRET = 18766, "relaykey", "lab-jobs", "b" * 64
NEG = f"http://127.0.0.1:{PORT}/negotiate?code={KEY}"
procs = []
stub = tmp / "stub"
(stub / "pyautogui").mkdir(parents=True)
(stub / "pyautogui" / "__init__.py").write_text("FAILSAFE = False\nPAUSE = 0\n")
env = dict(os.environ, PYTHONPATH=str(stub))
try:
    procs.append(subprocess.Popen([sys.executable, str(ROOT / "local_relay.py"), "--port", str(PORT), "--key", KEY,
                                   "--host", "127.0.0.1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
    import socket as _s
    for _ in range(100):
        try:
            _s.create_connection(("127.0.0.1", PORT), 0.2).close(); break
        except OSError:
            time.sleep(0.1)
    cfg = tmp / "agent.json"
    cfg.write_text(json.dumps({"negotiate_url": NEG, "group": GROUP, "secret": SECRET, "jobs_dir": str(tmp / "agentjobs"),
                               "tls_guard": "enforce"}))
    alog = tmp / "agent.log"
    procs.append(subprocess.Popen([sys.executable, str(ROOT / "nano_agent.py"), "--config", str(cfg), "--log", str(alog)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
    from nano_client import NanoClient, NanoError
    from nano_proto import key_from_secret
    with NanoClient(NEG, GROUP, key_from_secret(SECRET), timeout=20) as c:
        info = c.wait_for_agent(30)
        check("agent reports version, elevation, pwsh, tls guard",
              info["agent"] == "1.2.0" and "elevated" in info and "pwsh" in info and info["tls_guard"] == "enforce", info)

        # jobs over the wire
        j = c.job_start(script="echo from-job; sleep 1; echo done-job", shell="bash", name="wire")
        check("job_start returns at once with an id", j["status"] == "running" and j["id"], j)
        st = wait_for(lambda: [x for x in c.job_status(j["id"]) if x["status"] != "running"], 20)
        check("job_status shows completion", st and st[0]["status"] == "done", st)
        tl = c.job_tail(j["id"], lines=10)
        check("job_tail returns the output", "done-job" in tl["output"]["text"], tl)
        check("job_status list is cheap", len(json.dumps(c.job_status())) < 3000)

        # run: pwsh option, timeout kills tree
        if shutil.which("pwsh"):
            rc_, out, err = c.run("Write-Output ('pw' + 'sh7')", shell="pwsh")
            check("run with shell=pwsh", rc_ == 0 and out.strip() == "pwsh7", (rc_, out, err))
            j = c.job_start(script="# relay: timeout=30\nGet-Date -Format yyyy; Write-Output 'pwsh job ok'", shell="pwsh")
            st = wait_for(lambda: [x for x in c.job_status(j["id"]) if x["status"] != "running"], 30)
            check("job runs under pwsh", st and st[0]["status"] == "done" and "pwsh job ok" in c.job_tail(j["id"])["output"]["text"], st)
        mk = tmp / "run_child.pid"
        t0 = time.time()
        rc_, out, err = c.run(f"(sleep 300 & echo $! > {mk}; wait)", timeout=2)
        time.sleep(0.6)
        check("run timeout returns promptly and kills the tree",
              time.time() - t0 < 12 and rc_ == -1 and "timed out" in err and not pid_alive(int(mk.read_text())), (rc_, err))

        # chunked files
        big = tmp / "big.bin"
        big.write_bytes(os.urandom(1_300_000))
        remote = str(tmp / "remote" / "big.bin")
        c.put_file(str(big), remote)
        check("chunked put (1.3 MB)", Path(remote).read_bytes() == big.read_bytes())
        c.get_file(remote, str(tmp / "big.down"))
        check("chunked get (1.3 MB)", (tmp / "big.down").read_bytes() == big.read_bytes())
        sh = c.file_sha256(remote)
        check("file_sha256 matches", sh["sha256"] == hashlib.sha256(big.read_bytes()).hexdigest() and sh["size"] == 1_300_000, sh)
        try:
            c.call("file_get", path=remote)
            check("oversize single get refused with advice", False)
        except NanoError as e:
            check("oversize single get refused with advice", "chunks" in str(e) and "tail" in str(e), e)
        n = c.get_file(remote, str(tmp / "tail.bin"), tail=1000)
        check("tail fetch returns only the end", n == 1000 and (tmp / "tail.bin").read_bytes() == big.read_bytes()[-1000:])
        small = tmp / "small.txt"; small.write_text("hello")
        c.put_file(str(small), str(tmp / "remote" / "small.txt"))
        c.get_file(str(tmp / "remote" / "small.txt"), str(tmp / "small.down"))
        check("small file still works", (tmp / "small.down").read_text() == "hello")

        # message accounting
        u = c.usage()
        check("controller counts messages/units", u["msgs_out"] > 10 and u["msgs_in"] > 10 and u["units"] >= u["msgs_out"] + u["msgs_in"], u)

        # redaction in the real agent log
        c.run("echo $((1+1)) # password = 'SuperSecret99'")
        try:
            c.call("type", text="TopSecretTyped")       # fails off-Windows without a display: that is fine
        except NanoError:
            pass
        time.sleep(0.5)
    text = alog.read_text(encoding="utf-8", errors="replace")
    check("agent log has commands", "cmd run" in text and "cmd job_start" in text)
    check("agent log never holds typed text or the password", "TopSecretTyped" not in text and "SuperSecret99" not in text)
finally:
    try:
        print('--- agent log ---'); print(alog.read_text()[-1500:])
    except Exception: pass
    for p in procs:
        p.terminate()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
