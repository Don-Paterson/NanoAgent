"""nano_jobs.py - detached, time-limited jobs for NanoAgent (no screen or network code).

A *job* is a script or command that keeps running on the lab machine whether or not a
controller is connected.  Output goes to a file on that machine, so a controller can
look in occasionally (a few small messages) instead of waiting on one open request.

    jobs\\<id>\\job.ps1      the script that was run (a command is saved as a script too)
    jobs\\<id>\\output.txt   everything it wrote (stdout and stderr together)
    jobs\\<id>\\meta.json    status: running | done | failed | timeout | killed | abandoned | ended

Header comments in a script are honoured, so scripts written for Lab-Relay run unchanged:

    # relay: timeout=5400 progress=60      (progress is ignored: the controller polls instead)
    # relay: watch=C:\\CCES-Automation-Logs\\*.log

The Lab-Relay helper functions (Save-RelayScreenshot, Save-RelayFile, Save-RelayText) are NOT
provided here; use NanoAgent's own screenshot and file operations instead.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

IS_WIN = os.name == "nt"
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 7200                      # 2 hours, same ceiling as Lab-Relay
MAX_OUTPUT_BYTES = 64 * 1024 * 1024     # a runaway job is stopped before it fills the disk
KEEP_DAYS = 14
TAIL_MAX_BYTES = 24_000                 # one tail reply stays small (costs ~12 Web PubSub units)
MAX_WATCH_FILES = 5

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- shells
def find_pwsh() -> Optional[str]:
    p = shutil.which("pwsh")
    if p:
        return p
    if IS_WIN:
        for c in (r"C:\Program Files\PowerShell\7\pwsh.exe", r"C:\Program Files\PowerShell\7-preview\pwsh.exe"):
            if os.path.isfile(c):
                return c
    return None


def resolve_shell(shell: str) -> str:
    """auto -> pwsh if installed, else powershell (Windows) / bash (elsewhere)."""
    shell = (shell or "auto").lower()
    if shell == "auto":
        if find_pwsh():
            return "pwsh"
        return "powershell" if IS_WIN else "bash"
    if shell not in ("pwsh", "powershell", "cmd", "bash"):
        raise ValueError(f"unknown shell '{shell}' (pwsh, powershell, cmd, bash, auto)")
    if shell == "pwsh" and not find_pwsh():
        raise FileNotFoundError("PowerShell 7 (pwsh) is not installed on this machine")
    return shell


def shell_argv(shell: str, command: Optional[str] = None, script_path: Optional[str] = None) -> list[str]:
    """argv for running inline `command` or the file `script_path` in `shell`."""
    shell = resolve_shell(shell)
    utf8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    if shell in ("pwsh", "powershell"):
        exe = find_pwsh() if shell == "pwsh" else "powershell.exe"
        base = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
        return base + (["-File", script_path] if script_path else ["-Command", utf8 + command])
    if shell == "cmd":
        return ["cmd.exe", "/d", "/c", script_path or command]
    return ["bash", script_path] if script_path else ["bash", "-lc", command]


def _popen_flags() -> dict:
    if IS_WIN:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(pid: int):
    """Kill a process and everything it started."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20,
                           creationflags=_CREATE_NO_WINDOW)
        else:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, subprocess.SubprocessError, OSError):
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie child is dead for our purposes
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def run_capture(argv: list[str], timeout: float) -> dict:
    """Run to completion and capture output; on timeout the whole process tree is killed."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         **_popen_flags())
    try:
        out, err = p.communicate(timeout=timeout)
        return {"rc": p.returncode, "out": out, "err": err, "timed_out": False}
    except subprocess.TimeoutExpired:
        kill_tree(p.pid)
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return {"rc": -1, "out": out or b"", "err": err or b"", "timed_out": True}


# --------------------------------------------------------------------------- header comments
_HDR = re.compile(r"^\s*#\s*relay:\s*(.+)$", re.I)


def parse_header(text: str) -> dict:
    """'# relay: timeout=N progress=N' and '# relay: watch=<glob>' (one per line) from the top of a script."""
    out = {"timeout": None, "progress": None, "watch": []}
    for line in text.splitlines()[:40]:
        m = _HDR.match(line)
        if not m:
            continue
        body = m.group(1).strip()
        if body.lower().startswith("watch="):
            out["watch"].append(body[6:].strip())
            continue
        for k, v in re.findall(r"(\w+)=(\S+)", body):
            k = k.lower()
            if k in ("timeout", "progress") and v.isdigit():
                out[k] = int(v)
    return out


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name or "job").strip("-")[:40] or "job"


# --------------------------------------------------------------------------- manager
class JobManager:
    def __init__(self, root: Path, log=None):
        self.root = Path(root) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._log = log
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._kill_flags: set[str] = set()

    def _say(self, msg, *a):
        if self._log:
            self._log.info(msg, *a)

    # ---- files
    def _dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", job_id or ""):
            raise ValueError("bad job id")
        d = self.root / job_id
        if not d.is_dir():
            raise FileNotFoundError(f"no such job: {job_id}")
        return d

    def _write_meta(self, d: Path, meta: dict):
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        os.replace(tmp, d / "meta.json")

    def _read_meta(self, d: Path) -> dict:
        try:
            return json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ---- start
    def start(self, command: Optional[str] = None, script: Optional[str] = None, shell: str = "auto",
              timeout: Optional[float] = None, watch=None, name: Optional[str] = None) -> dict:
        text = script if script is not None else command
        if not text or not text.strip():
            raise ValueError("job_start needs a script or a command")
        sh = resolve_shell(shell)
        hdr = parse_header(text)
        t = timeout if timeout is not None else hdr["timeout"] or DEFAULT_TIMEOUT
        t = int(max(5, min(float(t), MAX_TIMEOUT)))
        watch_globs = list(watch or []) + hdr["watch"]
        job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_safe(name)}-{uuid.uuid4().hex[:4]}"
        d = self.root / job_id
        d.mkdir(parents=True)
        ext = {"pwsh": ".ps1", "powershell": ".ps1", "cmd": ".cmd", "bash": ".sh"}[sh]
        sp = d / f"job{ext}"
        # BOM so Windows PowerShell 5.1 reads non-ASCII correctly; harmless for pwsh
        sp.write_bytes((b"\xef\xbb\xbf" if sh in ("pwsh", "powershell") else b"") + text.encode("utf-8"))
        argv = shell_argv(sh, script_path=str(sp))
        env = dict(os.environ, NANO_JOB_ID=job_id, NANO_JOB_DIR=str(d))
        out = open(d / "output.txt", "wb")
        flags = _popen_flags()
        try:
            try:
                p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                     cwd=str(d), env=env,
                                     **({"creationflags": flags["creationflags"] | _CREATE_BREAKAWAY_FROM_JOB}
                                        if IS_WIN else flags))
            except OSError:        # breakaway not permitted by the parent's job object
                p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                     cwd=str(d), env=env, **flags)
        finally:
            out.close()
        meta = {"id": job_id, "name": name or "", "shell": sh, "status": "running", "pid": p.pid,
                "started": _utc(), "started_epoch": time.time(), "timeout": t, "watch": watch_globs,
                "rc": None, "finished": None, "dir": str(d), "output": str(d / "output.txt")}
        self._write_meta(d, meta)
        with self._lock:
            self._procs[job_id] = p
        threading.Thread(target=self._monitor, args=(d, meta, p), daemon=True, name=f"job-{job_id}").start()
        self._say("job %s started pid=%s shell=%s timeout=%ss", job_id, p.pid, sh, t)
        return self._brief(meta)

    # ---- monitor
    def _finish(self, d: Path, meta: dict, status: str, rc):
        meta.update(status=status, rc=rc, finished=_utc())
        self._write_meta(d, meta)
        self._say("job %s %s rc=%s", meta["id"], status, rc)

    def _monitor(self, d: Path, meta: dict, p: Optional[subprocess.Popen]):
        job_id, pid = meta["id"], meta["pid"]
        deadline = meta["started_epoch"] + meta["timeout"]
        out_path = d / "output.txt"
        while True:
            rc = p.poll() if p is not None else (None if pid_alive(pid) else "gone")
            if rc is not None:
                if p is None:
                    self._finish(d, meta, "ended", None)         # recovered job: exit code not known
                else:
                    self._finish(d, meta, "done" if rc == 0 else "failed", rc)
                break
            if job_id in self._kill_flags:
                kill_tree(pid)
                if p is not None:
                    try:
                        p.wait(10)
                    except subprocess.TimeoutExpired:
                        pass
                self._finish(d, meta, "killed", p.returncode if p is not None else None)
                break
            if time.time() > deadline:
                kill_tree(pid)
                if p is not None:
                    try:
                        p.wait(10)
                    except subprocess.TimeoutExpired:
                        pass
                self._finish(d, meta, "timeout", p.returncode if p is not None else None)
                break
            try:
                if out_path.stat().st_size > MAX_OUTPUT_BYTES:
                    kill_tree(pid)
                    self._finish(d, meta, "killed", None)
                    break
            except OSError:
                pass
            time.sleep(0.5)
        with self._lock:
            self._procs.pop(job_id, None)
            self._kill_flags.discard(job_id)

    def recover(self):
        """Called when the agent starts: re-attach to jobs that outlived the previous agent."""
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            meta = self._read_meta(d)
            if meta.get("status") != "running":
                continue
            if pid_alive(int(meta.get("pid", 0))):
                self._say("job %s still running; re-attaching", meta["id"])
                threading.Thread(target=self._monitor, args=(d, meta, None), daemon=True).start()
            else:
                self._finish(d, meta, "abandoned", None)
        self._purge()

    def _purge(self):
        cutoff = time.time() - KEEP_DAYS * 86400
        for d in self.root.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff and self._read_meta(d).get("status") != "running":
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    # ---- queries
    @staticmethod
    def _brief(meta: dict) -> dict:
        elapsed = (time.time() - meta["started_epoch"]) if meta.get("status") == "running" else None
        if elapsed is None and meta.get("finished"):
            try:
                end = datetime.strptime(meta["finished"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                elapsed = end.timestamp() - meta["started_epoch"]
            except ValueError:
                pass
        size = None
        try:
            size = os.path.getsize(meta["output"])
        except (OSError, KeyError):
            pass
        return {"id": meta["id"], "name": meta.get("name", ""), "status": meta["status"], "rc": meta.get("rc"),
                "shell": meta.get("shell"), "started": meta.get("started"), "finished": meta.get("finished"),
                "elapsed": round(elapsed, 1) if elapsed is not None else None, "timeout": meta.get("timeout"),
                "output_bytes": size, "output": meta.get("output")}

    def status(self, job_id: Optional[str] = None, limit: int = 10) -> dict:
        if job_id:
            return {"jobs": [self._brief(self._read_meta(self._dir(job_id)))]}
        metas = [self._read_meta(d) for d in sorted(self.root.iterdir(), reverse=True) if d.is_dir()]
        return {"jobs": [self._brief(m) for m in metas[:limit] if m.get("id")]}

    @staticmethod
    def _tail_file(path: str, lines: int, max_bytes: int) -> dict:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - max_bytes * 4))
                raw = f.read()
        except OSError as e:
            return {"path": path, "error": str(e)}
        text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
        tail = "\n".join(text.split("\n")[-lines:])
        cut = len(tail) > max_bytes
        return {"path": path, "size": size, "text": tail[-max_bytes:], "truncated": cut or size > len(raw)}

    def tail(self, job_id: str, lines: int = 40, max_bytes: int = TAIL_MAX_BYTES, watch: bool = True) -> dict:
        d = self._dir(job_id)
        meta = self._read_meta(d)
        lines = int(max(1, min(lines, 2000)))
        max_bytes = int(max(500, min(max_bytes, 120_000)))
        res = {"job": self._brief(meta), "output": self._tail_file(str(d / "output.txt"), lines, max_bytes)}
        if watch and meta.get("watch"):
            files = []
            for g in meta["watch"]:
                files += sorted(glob.glob(os.path.expandvars(g)), key=os.path.getmtime, reverse=True)
            res["watched"] = [self._tail_file(p, lines, max_bytes // 2) for p in files[:MAX_WATCH_FILES]]
        return res

    def kill(self, job_id: str) -> dict:
        d = self._dir(job_id)
        meta = self._read_meta(d)
        if meta.get("status") != "running":
            return {"job": self._brief(meta), "note": "not running"}
        with self._lock:
            self._kill_flags.add(job_id)
        for _ in range(40):
            time.sleep(0.25)
            meta = self._read_meta(d)
            if meta.get("status") != "running":
                break
        return {"job": self._brief(meta)}
